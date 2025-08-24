# -*- coding: utf-8 -*-
# ------------------------------------------------------------
# Discord 분배 봇 (리팩토링본)
# - 모든 쓰기 작업(편집/삭제/리액션/스레드초대/DM)을 단일 큐를 통해 직렬화
# - 임베드 편집 디바운스(동시 반응 폭주 시 1.5초에 1회로 합쳐서 편집)
# - DM 옵트인 + 사용자 쿨다운 (스팸/남발 방지)
# - 부팅 시 / 명령어 sync는 환경변수로 제어(SYNC_ON_STARTUP=1 이면 실행)
# - 반응 처리 사용자별 쿨다운(1.5초)
# Python 3.9 호환을 위해 typing.Optional 사용 (PEP604 X)
# ------------------------------------------------------------

import os
import asyncio
import random
from typing import Optional, Dict, Set, Tuple, List

import discord
from discord.ext import commands
from discord import app_commands

from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# keepalive 서버는 루트(/)에 200을 반환하도록 구성해주세요.
# (UptimeRobot이 HEAD/GET으로 상태 확인 시 200이 떠야 503로 치지 않습니다)
from keepalive import keep_alive

# ================== 기본 설정 ==================
keep_alive()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

KST = ZoneInfo("Asia/Seoul")
def now_kst():
    return datetime.now(KST)

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

# 숫자 이모지(최대 10명), 완료/판매 이모지
emoji_list = ['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣','🔟']
check_emoji = '✅'
sell_emoji  = '💰'

# 완료 채널 ID (필요에 맞게 설정)
완료_채널_ID = 1399368173949550692

# 분배 상태 메모리
distribution_data: Dict[int, dict] = {}
delete_delay = 10

# ====== 레이트리밋/큐 설정 ======
DELAY_JITTER_RANGE = (0.00, 0.15)
INVITE_DELAY_BASE   = 0.30
REACTION_DELAY_BASE = 0.25
DM_DELAY_BASE       = 1.00
ACTION_DELAY_BASE   = 0.10

MAX_REACTIONS_PER_MESSAGE = 12

# 임베드 편집 디바운스 윈도우 (초)
UPDATE_WINDOW = 1.5

# 사용자별 리액션 처리 쿨다운(초)
REACTION_COOLDOWN = 1.5

# DM 관련 (옵트인 + 쿨다운)
opt_in_users: Set[int] = set()         # DM 수신 동의한 사용자 ID 집합
USER_DM_COOLDOWN = 3600                # 1시간에 1회 (유저별)
last_user_dm: Dict[int, float] = {}    # user_id -> monotonic_ts

def with_jitter(base: float) -> float:
    lo, hi = DELAY_JITTER_RANGE
    return base + random.uniform(lo, hi)

async def pace(base: float):
    await asyncio.sleep(with_jitter(base))

# ================== 유틸 ==================
async def _safe_delete_impl(msg: discord.Message, delay=delete_delay):
    """큐 내부에서 실행되는 안전 삭제(지연 후 삭제)."""
    try:
        await asyncio.sleep(delay)
        await msg.delete()
    except Exception as e:
        print(f"[WARN] 메시지 삭제 실패: {e}")

# ================== Bot ==================
class MyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bg_queue: Optional[asyncio.Queue] = None
        self.bg_worker_task: Optional[asyncio.Task] = None

        # 디바운스 관리: msg_id -> asyncio.Task
        self.update_tasks: Dict[int, asyncio.Task] = {}

        # (msg_id, user_id) -> last_ts
        self.last_reaction_ts: Dict[Tuple[int, int], float] = {}

    async def setup_hook(self):
        # 큐/워커를 현재 이벤트 루프에서 생성
        self.bg_queue = asyncio.Queue()
        self.bg_worker_task = asyncio.create_task(self.background_worker())

        # / 명령어 동기화: 환경변수로 제어
        try:
            if os.getenv("SYNC_ON_STARTUP") == "1":
                # 개발시 특정 길드만 싱크 권장: GUILD_SYNC_ID 사용
                guild_id_str = os.getenv("GUILD_SYNC_ID")
                if guild_id_str:
                    gobj = discord.Object(id=int(guild_id_str))
                    synced = await self.tree.sync(guild=gobj)
                    print(f"✅ 길드({guild_id_str}) 슬래시 {len(synced)}개 동기화 완료")
                else:
                    synced = await self.tree.sync()
                    print(f"✅ 전역 슬래시 {len(synced)}개 동기화 완료")
            else:
                print("ℹ️ SYNC_ON_STARTUP=1 이 아니므로 /명령어 동기화 생략")
        except Exception as e:
            print(f"❌ 슬래시 명령어 동기화 실패/생략: {e}")

    async def background_worker(self):
        """전역 백그라운드 큐 1개 워커로 순차 처리"""
        assert self.bg_queue is not None
        while True:
            job_coro = await self.bg_queue.get()
            try:
                await job_coro
            except Exception as e:
                print(f"[ERROR] bg job 실패: {e}")
            finally:
                self.bg_queue.task_done()
                await pace(ACTION_DELAY_BASE)

    async def enqueue_bg(self, coro):
        """큐에 작업 등록 (모든 쓰기 작업은 여기로)"""
        assert self.bg_queue is not None
        await self.bg_queue.put(coro)

    # --------------- 공용 쓰기 래퍼 ---------------
    async def enqueue_edit_message(self, message: discord.Message, embed: discord.Embed):
        await self.enqueue_bg(message.edit(embed=embed))

    async def enqueue_add_reaction(self, message: discord.Message, emoji: str):
        async def _job():
            await message.add_reaction(emoji)
        await self.enqueue_bg(_job())

    async def enqueue_thread_add_user(self, thread: discord.Thread, member: discord.Member):
        async def _job():
            await thread.add_user(member)
        await self.enqueue_bg(_job())

    async def enqueue_send(self, channel: discord.abc.Messageable, *args, **kwargs):
        """
        주의: 반환값(Message)이 필요하면 이 래퍼 대신 직접 await channel.send(...) 사용.
        이 래퍼는 큐로만 흘려보내므로 반환값이 없다.
        """
        async def _job():
            await channel.send(*args, **kwargs)
        await self.enqueue_bg(_job())

    async def enqueue_delete(self, message: discord.Message, delay: int = delete_delay):
        await self.enqueue_bg(_safe_delete_impl(message, delay=delay))

    async def enqueue_thread_delete(self, thread: discord.Thread):
        async def _job():
            await thread.delete()
        await self.enqueue_bg(_job())

# Bot 인스턴스
bot = MyBot(command_prefix="!", intents=intents)

# ================== 게시글 즉시 표시 ==================
async def create_distribution(channel: discord.TextChannel, author: discord.Member, item: str, mention_list: List[discord.Member]):
    safe_mentions = mention_list[:10]  # 숫자 이모지 최대 10명
    lines = [f"{emoji_list[i]} {m.mention}" for i, m in enumerate(safe_mentions)]

    now = now_kst()
    date_str = now.strftime('%m/%d')
    time_str = now.strftime('%p %I:%M').replace('AM','오전').replace('PM','오후')

    embed = discord.Embed(title="🍆 아이템 분배 안내", color=0x9146FF)
    summary = f"🎁 아이템명 : {item}\n📅 날짜 및 시간 : {date_str} {time_str}\n👤 생성자 : {author.mention}"
    embed.add_field(name="ℹ️ 기본 정보", value=summary, inline=False)
    embed.add_field(name="🎯 수령 대상자", value="\n".join(lines) if lines else "등록된 대상자가 없습니다.", inline=False)
    embed.add_field(name="📢 사용법", value="🔸 번호 이모지 누르면 수령 처리!\n✅ 누르면 완료게시판으로 이동!\n💰 누르면 판매완료 DM 전송!", inline=False)
    embed.add_field(name="💸 판매금액", value="미입력", inline=False)

    # 최초 메시지는 즉시 전송(반환 Message 필요)
    msg = await channel.send(embed=embed)

    # 제목에 메시지 ID 반영 (이 편집은 큐를 통해)
    embed.title = f"🍆 아이템 분배 안내 (ID: {msg.id})"
    await bot.enqueue_edit_message(msg, embed)

    distribution_data[msg.id] = {
        "creator": author,
        "mentions": safe_mentions,
        "received": set(),
        "message": msg,
        "embed": embed,
        "item": item,
        "datetime": now,
        "price": "미입력"
    }

    # 느린 작업(스레드/초대/리액션)은 큐로 순차 처리
    await bot.enqueue_bg(background_finalize(msg, item, author, safe_mentions, embed))

# ================== 백그라운드 작업 ==================
async def background_finalize(message: discord.Message, item: str, author: discord.Member, mention_list: List[discord.Member], embed: discord.Embed):
    """스레드 생성/초대 + 이모지 추가 (전부 큐에서 실행됨)"""
    try:
        thread = await message.create_thread(name=f"{item} 분배", auto_archive_duration=60)
        await pace(ACTION_DELAY_BASE)

        try:
            await thread.add_user(author)
        except Exception as e:
            print(f"[WARN] thread.add_user(author) 실패: {e}")
        await pace(INVITE_DELAY_BASE)

        for m in mention_list:
            try:
                await thread.add_user(m)
            except Exception as e:
                print(f"[WARN] thread.add_user({m}) 실패: {e}")
            await pace(INVITE_DELAY_BASE)

        num_reactions = min(len(mention_list), len(emoji_list), MAX_REACTIONS_PER_MESSAGE)
        for i in range(num_reactions):
            try:
                await message.add_reaction(emoji_list[i])
            except Exception as e:
                print(f"[WARN] add_reaction 숫자 실패: {e}")
            await pace(REACTION_DELAY_BASE)

        added = num_reactions
        if added < MAX_REACTIONS_PER_MESSAGE:
            try:
                await message.add_reaction(check_emoji)
            except Exception as e:
                print(f"[WARN] add_reaction 체크 실패: {e}")
            await pace(REACTION_DELAY_BASE)
            added += 1
        if added < MAX_REACTIONS_PER_MESSAGE:
            try:
                await message.add_reaction(sell_emoji)
            except Exception as e:
                print(f"[WARN] add_reaction 판매 실패: {e}")

    except Exception as e:
        print(f"[ERROR] background_finalize 실패: {e}")

async def background_notify_sale(guild_id: int, channel_id: int, message_id: int, creator_name: str, mentions: List[discord.Member], item: str):
    """판매완료 DM 전송 (전부 큐에서 실행됨)
       - 옵트인 사용자만
       - 유저별 1시간 쿨다운
    """
    msg_link = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    now = asyncio.get_running_loop().time()

    for m in mentions:
        if m.bot:
            continue
        if m.id not in opt_in_users:
            # 옵트인 안 했으면 건너뜀
            continue
        last_ts = last_user_dm.get(m.id, 0.0)
        if now - last_ts < USER_DM_COOLDOWN:
            # 쿨다운 중이면 건너뜀
            continue

        try:
            await m.send(
                f"👤 {creator_name} 님의 분배 게시자입니다.\n"
                f"💰 `{item}` 아이템이 판매 완료되었어요!\n"
                f"🔗 [바로가기]({msg_link})"
            )
            last_user_dm[m.id] = asyncio.get_running_loop().time()
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"[WARN] DM 실패({m}): {e}")
        await pace(DM_DELAY_BASE)

# ================== 임베드 편집 디바운스 ==================
async def schedule_embed_update(msg_id: int):
    """
    반응 폭주 시 임베드 편집을 합쳐서 1.5초 후 1회만 수행.
    """
    # 기존 스케줄이 있으면 취소
    old = bot.update_tasks.get(msg_id)
    if old and not old.done():
        old.cancel()

    async def _job():
        try:
            await asyncio.sleep(UPDATE_WINDOW)
            if msg_id not in distribution_data:
                return
            data = distribution_data[msg_id]
            message: discord.Message = data["message"]
            embed: discord.Embed = data["embed"]
            mentions: List[discord.Member] = data["mentions"]
            received: Set[int] = data["received"]

            lines = []
            for i, m in enumerate(mentions):
                line = f"{emoji_list[i]} {m.mention}"
                if i in received:
                    line += " ✅"
                lines.append(line)

            embed.set_field_at(1, name="🎯 수령 대상자", value="\n".join(lines) if lines else "등록된 대상자가 없습니다.", inline=False)
            await bot.enqueue_edit_message(message, embed)
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[WARN] 디바운스 업데이트 실패: {e}")

    task = asyncio.create_task(_job())
    bot.update_tasks[msg_id] = task

# ================== 느낌표 명령어 ==================
@bot.command()
async def 분배(ctx: commands.Context, *, arg):
    # 원본 명령 메시지 즉시 삭제는 반환 Message 필요 → 직접 await, 삭제는 큐로
    await bot.enqueue_delete(ctx.message)
    try:
        item, _ = map(str.strip, arg.split('/', 1))
        mention_list = ctx.message.mentions
        await create_distribution(ctx.channel, ctx.author, item, mention_list)
    except Exception as e:
        m = await ctx.send(f"❌ 오류 발생: {e}")
        await bot.enqueue_delete(m)

@bot.command()
async def 판매(ctx: commands.Context, message_id: int, *, content: str):
    await bot.enqueue_delete(ctx.message)
    try:
        if message_id in distribution_data:
            msg_data = distribution_data[message_id]
            msg_data['price'] = content
            embed = msg_data['embed']
            # 3번째 필드(인덱스 3) = 판매금액 갱신
            embed.set_field_at(index=3, name="💸 판매금액", value=content, inline=False)
            await bot.enqueue_edit_message(msg_data['message'], embed)
            m = await ctx.send(f"💸 판매금액이 등록되었습니다: `{content}`")
            await bot.enqueue_delete(m)
        else:
            m = await ctx.send("❌ 해당 메시지를 찾을 수 없습니다.")
            await bot.enqueue_delete(m)
    except Exception as e:
        m = await ctx.send(f"❌ 오류: {e}")
        await bot.enqueue_delete(m)

@bot.command(name="ㅍ")
async def 판매_축약(ctx: commands.Context, message_id: int, *, content: str):
    await bot.enqueue_delete(ctx.message)
    await 판매(ctx, message_id, content=content)

@bot.command()
async def 분배중(ctx: commands.Context):
    await bot.enqueue_delete(ctx.message)
    await send_distribution_list(ctx.author, ctx.guild, ctx.channel, exclude_completed=True)

@bot.command()
async def 알림동의(ctx: commands.Context):
    """DM 옵트인 토글"""
    await bot.enqueue_delete(ctx.message)
    uid = ctx.author.id
    if uid in opt_in_users:
        opt_in_users.remove(uid)
        m = await ctx.send(f"🔕 {ctx.author.mention} DM 알림 동의가 해제되었습니다.")
    else:
        opt_in_users.add(uid)
        m = await ctx.send(f"🔔 {ctx.author.mention} DM 알림 동의가 설정되었습니다.")
    await bot.enqueue_delete(m)

# ================== 슬래시 명령어 ==================
@bot.tree.command(name="분배", description="아이템 분배 등록")
@app_commands.describe(item="아이템 이름", 대상자="수령 대상자 멘션(e.g. @사용자1 @사용자2)")
async def slash_분배(interaction: discord.Interaction, item: str, 대상자: str):
    # 응답 타임아웃 방지
    try:
        await interaction.response.send_message("🛠 등록 중입니다...", ephemeral=True)
    except Exception:
        pass

    # 멘션 파싱: 채널 멤버 중 텍스트에 포함된 멘션만 추출
    mention_list = [m for m in interaction.channel.members if f"<@{m.id}>" in 대상자 or f"<@!{m.id}>" in 대상자]
    await create_distribution(interaction.channel, interaction.user, item, mention_list)

    try:
        await interaction.followup.send("✅ 분배 등록 완료!", ephemeral=True)
    except Exception:
        pass

@bot.tree.command(name="분배중", description="내가 수령자에 포함된 미완료 분배 목록 DM 전송")
async def slash_분배중(interaction: discord.Interaction):
    try:
        await interaction.response.send_message("📬 DM으로 전송할게요!", ephemeral=True)
    except Exception:
        pass
    await send_distribution_list(interaction.user, interaction.guild, interaction.channel, exclude_completed=True)

@bot.tree.command(name="알림동의", description="판매 완료 DM 알림 수신 동의/해제 토글")
async def slash_알림동의(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid in opt_in_users:
        opt_in_users.remove(uid)
        await interaction.response.send_message("🔕 DM 알림 동의가 해제되었습니다.", ephemeral=True)
    else:
        opt_in_users.add(uid)
        await interaction.response.send_message("🔔 DM 알림 동의가 설정되었습니다.", ephemeral=True)

# ================== 리액션 이벤트 ==================
@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    if user.bot:
        return
    await handle_reaction_event(reaction, user, is_add=True)

@bot.event
async def on_reaction_remove(reaction: discord.Reaction, user: discord.User):
    if user.bot:
        return
    await handle_reaction_event(reaction, user, is_add=False)

async def handle_reaction_event(reaction: discord.Reaction, user: discord.User, is_add: bool):
    msg_id = reaction.message.id
    if msg_id not in distribution_data:
        return

    # 사용자별 반응 쿨다운
    key = (msg_id, user.id)
    now = asyncio.get_running_loop().time()
    last_ts = bot.last_reaction_ts.get(key, 0.0)
    if now - last_ts < REACTION_COOLDOWN:
        return
    bot.last_reaction_ts[key] = now

    data = distribution_data[msg_id]
    emoji = str(reaction.emoji)
    message: discord.Message = data["message"]
    embed: discord.Embed = data["embed"]
    guild = message.guild
    완료채널 = guild.get_channel(완료_채널_ID)

    async def 종료처리():
        try:
            if 완료채널:
                await bot.enqueue_send(완료채널, embed=embed)
            if hasattr(message, "thread") and message.thread:
                await bot.enqueue_thread_delete(message.thread)
            await bot.enqueue_delete(message, delay=0)  # 즉시 삭제
        except Exception as e:
            print(f"[ERROR] 종료 처리 중 오류: {e}")

    if emoji in emoji_list:
        index = emoji_list.index(emoji)

        # 수령 집합 갱신
        if is_add:
            data["received"].add(index)
        else:
            data["received"].discard(index)

        # 임베드 편집은 디바운스로 합쳐서 실행
        await schedule_embed_update(msg_id)

        # 전원 수령 완료 시 종료
        if is_add and len(data["received"]) == len(data["mentions"]) and len(data["mentions"]) > 0:
            tmp = await message.channel.send("✅ 모든 대상자 수령 완료. 분배 종료!")
            await bot.enqueue_delete(tmp)
            await 종료처리()
            distribution_data.pop(msg_id, None)

    elif is_add and emoji == sell_emoji:
        tmp = await message.channel.send("💰 판매 완료! (DM은 알림동의한 분들만 전송됩니다)")
        await bot.enqueue_delete(tmp)
        creator = data["creator"].display_name
        # DM은 큐에 태움
        await bot.enqueue_bg(background_notify_sale(
            guild.id, message.channel.id, message.id, creator, data["mentions"], data["item"]
        ))

    elif is_add and emoji == check_emoji:
        tmp = await message.channel.send("✅ 강제 종료 처리되었습니다.")
        await bot.enqueue_delete(tmp)
        await 종료처리()
        distribution_data.pop(msg_id, None)

# ================== 분배 목록 DM ==================
async def send_distribution_list(user: discord.User, guild: discord.Guild, channel: discord.abc.Messageable, exclude_completed: bool = True):
    """
    exclude_completed=True:
      본인 이름 옆에 ✅가 붙어 있으면(본인/타인 누름 상관없이) DM 목록에서 제외.
    """
    found = []
    for msg_id, data in distribution_data.items():
        mentions: List[discord.Member] = data["mentions"]
        if user in mentions:
            try:
                idx = mentions.index(user)
            except ValueError:
                continue
            if exclude_completed and idx in data["received"]:
                continue

            dt = data["datetime"]
            date_str = dt.strftime('%m/%d')
            time_str = dt.strftime('%p %I:%M').replace('AM','오전').replace('PM','오후')
            author = data["creator"].display_name
            link = f"https://discord.com/channels/{guild.id}/{data['message'].channel.id}/{msg_id}"
            found.append(f"{data['item']} | 🕛 {date_str} ⏰ {time_str} 👤 {author}\n → [바로가기]({link})")

    if found:
        try:
            await user.send("\n".join([f"📄 {user.display_name} 님의 분배 목록 (완료 제외):"] + found))
        except discord.Forbidden:
            m = await channel.send(f"⚠️ {user.mention}님에게 DM을 보내지 못했습니다.")
            await bot.enqueue_delete(m, delay=5)
    else:
        m = await channel.send(f"🔍 {user.mention}님이 확인할 미완료 분배 항목이 없습니다.")
        await bot.enqueue_delete(m, delay=5)

# ================== 실행 ==================
if not TOKEN:
    raise SystemExit("환경변수 DISCORD_TOKEN 이 비어있습니다.")

bot.run(TOKEN)