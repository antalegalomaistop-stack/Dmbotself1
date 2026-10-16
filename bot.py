import os
import re
import json
import asyncio
import time
import random
import traceback
from datetime import datetime
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv()

# ==================== CONFIG ====================
TOKEN = os.getenv("USER_TOKEN")
PREFIX = os.getenv("PREFIX", "!")
DELAY = float(os.getenv("DELAY", "8.0"))
CHUNK = 1900
MAX_RETRIES = 2
LONG_PAUSE = float(os.getenv("LONG_PAUSE", "120"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
BATCH_PAUSE = float(os.getenv("BATCH_PAUSE", "60"))
BLACKLIST_FILE = Path("blacklist.json")

if not TOKEN:
    raise SystemExit("❌ Defina USER_TOKEN no arquivo .env")

client = discord.Client()

INVITE_RE = re.compile(
    r"(?:discord(?:app)?\.com/invite/|discord\.gg/)([A-Za-z0-9\-]+)",
    re.IGNORECASE
)

running_task = None
should_stop = False
waiting_captcha = False
captcha_event = asyncio.Event()


# -------------------- Blacklist --------------------
def load_blacklist() -> set:
    if BLACKLIST_FILE.exists():
        try:
            data = json.loads(BLACKLIST_FILE.read_text(encoding="utf-8"))
            return set(int(x) for x in data)
        except Exception:
            return set()
    return set()


def save_blacklist(bl: set):
    BLACKLIST_FILE.write_text(
        json.dumps(sorted(bl), indent=2),
        encoding="utf-8"
    )


blacklist = load_blacklist()


# -------------------- Helpers --------------------
def split_message(text: str, size: int = CHUNK) -> list:
    if not text:
        return [""]
    return [text[i:i + size] for i in range(0, len(text), size)]


def format_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def random_delay(base: float) -> float:
    return base + random.uniform(1.0, 3.0)


def is_real_captcha(error: Exception) -> bool:
    return type(error).__name__ == "CaptchaRequired"


def aplicar_variaveis(texto: str, membro: discord.Member) -> str:
    """Substitui variáveis pelo dados do membro."""
    nome = membro.display_name or membro.name or "amigo"
    username = membro.name or "user"
    tag = str(membro)
    return (
        texto
        .replace("{nome}", nome)
        .replace("{username}", username)
        .replace("{tag}", tag)
        .replace("{id}", str(membro.id))
    )


@client.event
async def on_ready():
    print("=" * 55)
    print(f"✅ Logado como: {client.user} ({client.user.id})")
    print(f"📌 Prefixo: {PREFIX}")
    print(f"⏱️  Delay: {DELAY}s | Lote: {BATCH_SIZE} → {BATCH_PAUSE}s")
    print(f"🚫 Blacklist: {len(blacklist)} usuário(s)")
    print(f"🔐 Captcha: MANUAL (!resolvido)")
    print(f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print("=" * 55)
    print("Comandos:")
    print(f"  {PREFIX}dm <msg> <link>        → todos")
    print(f"  {PREFIX}dmonline <msg> <link>  → só online")
    print(f"  {PREFIX}stop")
    print(f"  {PREFIX}resolvido")
    print(f"  {PREFIX}blacklist add/remove/list/clear")
    print("=" * 55)
    print("Variáveis na mensagem: {nome} {username} {tag} {id}")
    print("=" * 55)


@client.event
async def on_message(message: discord.Message):
    global running_task, should_stop, waiting_captcha, blacklist

    if message.author.id != client.user.id:
        return

    content = message.content.strip()
    lower = content.lower()

    # ========== STOP ==========
    if lower in (f"{PREFIX}stop", f"{PREFIX}cancelar"):
        if running_task and not running_task.done():
            should_stop = True
            waiting_captcha = False
            captcha_event.set()
            await message.reply("🛑 Parando...")
        else:
            await message.reply("Nenhum envio em andamento.")
        return

    # ========== RESOLVIDO ==========
    if lower in (f"{PREFIX}resolvido", f"{PREFIX}continuar", f"{PREFIX}continue", f"{PREFIX}ok"):
        if waiting_captcha:
            waiting_captcha = False
            captcha_event.set()
            await message.reply("✅ Ok, continuando em 20 segundos...")
        else:
            await message.reply("Não estou esperando captcha no momento.")
        return

    # ========== BLACKLIST ==========
    if lower.startswith(f"{PREFIX}blacklist"):
        partes = content.split()
        if len(partes) == 1:
            await message.reply(
                f"**Blacklist** ({len(blacklist)} usuários)\n"
                f"`{PREFIX}blacklist add <id>`\n"
                f"`{PREFIX}blacklist remove <id>`\n"
                f"`{PREFIX}blacklist list`\n"
                f"`{PREFIX}blacklist clear`"
            )
            return

        acao = partes[1].lower()

        if acao == "list":
            if not blacklist:
                await message.reply("Blacklist vazia.")
            else:
                lista = "\n".join(f"- `{uid}`" for uid in sorted(blacklist)[:50])
                extra = f"\n... e mais {len(blacklist)-50}" if len(blacklist) > 50 else ""
                await message.reply(f"**Blacklist** ({len(blacklist)}):\n{lista}{extra}")
            return

        if acao == "clear":
            blacklist.clear()
            save_blacklist(blacklist)
            await message.reply("✅ Blacklist limpa.")
            return

        if acao in ("add", "remove", "del", "rm") and len(partes) >= 3:
            try:
                uid = int(partes[2].strip("<@!>"))
            except ValueError:
                await message.reply("ID inválido.")
                return

            if acao == "add":
                blacklist.add(uid)
                save_blacklist(blacklist)
                await message.reply(f"✅ `{uid}` adicionado à blacklist.")
            else:
                blacklist.discard(uid)
                save_blacklist(blacklist)
                await message.reply(f"✅ `{uid}` removido da blacklist.")
            return

        await message.reply("Uso inválido. Digite só `!blacklist` pra ver os comandos.")
        return

    # ========== DM / DMONLINE ==========
    is_online_only = lower.startswith(f"{PREFIX}dmonline")
    is_dm = lower.startswith(f"{PREFIX}dm") and not is_online_only

    if not is_dm and not is_online_only:
        return

    cmd = f"{PREFIX}dmonline" if is_online_only else f"{PREFIX}dm"
    resto = content[len(cmd):]
    if resto and not resto[0].isspace():
        return

    corpo = resto.strip()
    match = INVITE_RE.search(corpo)
    if not match:
        await message.reply(
            f"**Uso:**\n"
            f"`{PREFIX}dm <mensagem> <link>` → todos\n"
            f"`{PREFIX}dmonline <mensagem> <link>` → só online\n\n"
            f"Variáveis: `{{nome}}` `{{username}}` `{{tag}}` `{{id}}`\n"
            f"Ex: `{PREFIX}dm Olá {{nome}}! discord.gg/abc`"
        )
        return

    invite_code = match.group(1)
    texto = (corpo[:match.start()] + corpo[match.end():]).strip(" |\n\t")

    if not texto and message.reference:
        try:
            citada = await message.channel.fetch_message(message.reference.message_id)
            texto = citada.content
        except Exception:
            pass

    if not texto:
        await message.reply("❌ Faltou o texto da mensagem.")
        return

    if running_task and not running_task.done():
        await message.reply("⚠️ Já tem um envio rodando. Use `!stop`.")
        return

    status = await message.reply("🔍 Resolvendo convite...")

    try:
        invite = await client.fetch_invite(invite_code, with_counts=True)
    except discord.NotFound:
        await status.edit(content="❌ Convite inválido ou expirado.")
        return
    except Exception as e:
        await status.edit(content=f"❌ Erro no convite: `{e}`")
        return

    guild = invite.guild
    if guild is None:
        await status.edit(content="❌ Não consegui ler o servidor.")
        return

    real_guild = client.get_guild(guild.id)
    if real_guild is None:
        try:
            real_guild = await client.fetch_guild(guild.id)
        except Exception:
            real_guild = None

    if real_guild is None:
        await status.edit(content=f"❌ Sua conta **não está** em **{guild.name}**.")
        return

    await status.edit(content=f"📥 Coletando membros de **{real_guild.name}**...")

    try:
        if hasattr(real_guild, "chunk"):
            await real_guild.chunk()
    except Exception as e:
        print(f"[chunk] {e}")

    membros = [m for m in real_guild.members if not m.bot and m.id != client.user.id]

    if len(membros) < 5:
        try:
            fetched = [
                m async for m in real_guild.fetch_members(limit=None)
                if not m.bot and m.id != client.user.id
            ]
            if fetched:
                membros = fetched
        except Exception as e:
            print(f"[fetch_members] {e}")

    # Filtro: blacklist
    antes = len(membros)
    membros = [m for m in membros if m.id not in blacklist]
    ignorados_bl = antes - len(membros)

    # Filtro: só online
    if is_online_only:
        membros = [m for m in membros if m.status != discord.Status.offline]

    if not membros:
        msg = "❌ Nenhum membro encontrado"
        if is_online_only:
            msg += " online"
        if ignorados_bl:
            msg += f" (blacklist removeu {ignorados_bl})"
        await status.edit(content=msg + ".")
        return

    random.shuffle(membros)

    tempo_est = len(membros) * (DELAY + 2) + (len(membros) // BATCH_SIZE) * BATCH_PAUSE
    tipo = "só online" if is_online_only else "todos"

    confirm_msg = await status.edit(
        content=(
            f"**Confirmação**\n"
            f"Servidor: **{real_guild.name}**\n"
            f"Modo: **{tipo}**\n"
            f"Membros: **{len(membros)}**"
            + (f" (blacklist: -{ignorados_bl})" if ignorados_bl else "")
            + f"\nDelay: **{DELAY}s** | Lote a cada {BATCH_SIZE}\n"
            f"Tempo estimado: **~{format_time(tempo_est)}**\n\n"
            f"Responda `sim` em 30s para começar."
        )
    )

    def check(m):
        return (
            m.author.id == client.user.id
            and m.channel.id == message.channel.id
            and m.id != confirm_msg.id
        )

    try:
        resposta = await client.wait_for("message", check=check, timeout=30)
    except asyncio.TimeoutError:
        await confirm_msg.edit(content="⏰ Tempo esgotado.")
        return

    if resposta.content.lower().strip() not in ("sim", "s", "yes", "y", "confirma"):
        await confirm_msg.edit(content="❌ Cancelado.")
        return

    should_stop = False
    waiting_captcha = False
    captcha_event.clear()
    running_task = asyncio.create_task(
        enviar_dms(real_guild, membros, texto, confirm_msg)
    )


async def enviar_uma_dm(membro: discord.Member, texto_base: str) -> str:
    try:
        texto = aplicar_variaveis(texto_base, membro)
        partes = split_message(texto)

        dm = membro.dm_channel
        if dm is None:
            dm = await membro.create_dm()

        for parte in partes:
            msg = await dm.send(parte)
            if msg is None or not getattr(msg, "id", None):
                return "erro"
        return "ok"

    except discord.Forbidden:
        return "fechada"

    except discord.HTTPException as e:
        if is_real_captcha(e):
            print(f"[CAPTCHA REAL] {membro}")
            return "captcha"

        status = getattr(e, "status", None)
        code = getattr(e, "code", None)

        if status == 429 or code == 429:
            print(f"[RATE LIMIT] {membro}")
            return "rate"

        if code in (50007, 50001, 40003, 40044):
            return "fechada"

        print(f"[HTTP {status}/{code}] {membro} → {e}")
        return "erro"

    except Exception as e:
        if is_real_captcha(e):
            print(f"[CAPTCHA REAL] {membro}")
            return "captcha"
        print(f"[ERRO] {membro} → {type(e).__name__}: {e}")
        return "erro"


async def enviar_dms(guild: discord.Guild, membros: list, texto: str, status: discord.Message):
    global should_stop, running_task, waiting_captcha

    total = len(membros)
    enviados = fechadas = erros = rate_limits = captchas = 0
    inicio = time.time()
    i = 0

    await status.edit(content=f"🚀 Enviando para **{total}** membros...")

    while i < total:
        if should_stop:
            break

        membro = membros[i]
        i += 1
        resultado = "erro"

        for tentativa in range(1, MAX_RETRIES + 1):
            if should_stop:
                break

            resultado = await enviar_uma_dm(membro, texto)

            if resultado == "ok":
                enviados += 1
                break

            if resultado == "fechada":
                fechadas += 1
                break

            if resultado == "rate":
                rate_limits += 1
                wait = LONG_PAUSE
                await status.edit(
                    content=f"⏳ Rate limit. Aguardando {wait:.0f}s... ({i}/{total})"
                )
                await asyncio.sleep(wait + random.uniform(3, 8))
                continue

            if resultado == "captcha":
                captchas += 1
                waiting_captcha = True
                captcha_event.clear()

                await status.edit(
                    content=(
                        f"🔐 **CAPTCHA REAL DETECTADO**\n"
                        f"────────────────────\n"
                        f"1. Abra o Discord no **navegador**\n"
                        f"2. Entre com a **mesma conta**\n"
                        f"3. Resolva o captcha\n"
                        f"4. Digite aqui: `{PREFIX}resolvido`\n\n"
                        f"Progresso: **{i}/{total}**"
                    )
                )
                print("=" * 50)
                print("🔐 CAPTCHA REAL – digite !resolvido depois de resolver")
                print("=" * 50)

                await captcha_event.wait()

                if should_stop:
                    break

                await status.edit(content="✅ Continuando em 20s...")
                await asyncio.sleep(20)
                i -= 1
                break

            if tentativa < MAX_RETRIES:
                await asyncio.sleep(3)
                continue
            erros += 1
            break

        if should_stop:
            break

        if resultado in ("ok", "fechada", "erro"):
            await asyncio.sleep(random_delay(DELAY))

        if not should_stop and i % BATCH_SIZE == 0 and i < total:
            await status.edit(content=f"⏸️ Pausa de lote ({i}/{total}) — {BATCH_PAUSE:.0f}s")
            await asyncio.sleep(BATCH_PAUSE)

        if i % 5 == 0 or i >= total or should_stop:
            elapsed = time.time() - inicio
            eta = (total - i) * (DELAY + 2) + ((total - i) // BATCH_SIZE) * BATCH_PAUSE
            try:
                await status.edit(
                    content=(
                        f"{'🛑 Parando' if should_stop else '📤 Enviando'} **{i}/{total}** ({i/total*100:.1f}%)\n"
                        f"✅ {enviados} | 🔒 {fechadas} | ❌ {erros}\n"
                        f"⏳ RL: {rate_limits} | 🔐 Captcha: {captchas}\n"
                        f"⏱️ {format_time(elapsed)} | ETA ~{format_time(eta)}"
                    )
                )
            except Exception:
                pass

    elapsed = time.time() - inicio
    motivo = " (cancelado)" if should_stop else ""

    try:
        await status.edit(
            content=(
                f"**✅ Finalizado{motivo}**\n"
                f"**{guild.name}**\n"
                f"────────────────────\n"
                f"✅ Enviadas: **{enviados}**\n"
                f"🔒 DM fechada: **{fechadas}**\n"
                f"❌ Erros: **{erros}**\n"
                f"⏳ Rate limits: **{rate_limits}**\n"
                f"🔐 Captchas: **{captchas}**\n"
                f"📊 Total: **{enviados + fechadas + erros}/{total}**\n"
                f"⏱️ Tempo: **{format_time(elapsed)}**"
            )
        )
    except Exception:
        pass

    should_stop = False
    waiting_captcha = False
    running_task = None


if __name__ == "__main__":
    try:
        client.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Token inválido.")
    except Exception as e:
        print(f"❌ Erro fatal: {e}")
        traceback.print_exc()
