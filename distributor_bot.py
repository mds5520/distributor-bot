import discord
import os
import asyncio
import random
from discord.ext import commands
from discord import app_commands
from datetime import datetime
from zoneinfo import ZoneInfo  # ✅ 한국 시간대
from dotenv import load_dotenv
from keepalive import keep_alive

# ================== 기본 설정 ==================
keep_alive()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ✅ 한국 시간대 사용
KST = ZoneInfo("Asia/Seoul")
def now_kst():
    return datetime.now(KST)

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

emoji_list = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
check_emoji = '✅'
sell_emoji = '💰'
완료_채널_ID = 1399368173949550692

distribution_data = {}
delete_delay = 10

# ================== 레이트리밋/큐 설정 ==================
# 전역 백그라운드 작업 큐: 스레드 생성, 초대, 이모지 추가, 대량 DM 전송 등을 "순차" 처리
bg_queue: asyncio.Queue = asyncio.Queue()

# 지터(무작위 지연) 범위
DELAY_JITTER_RANGE = (0.00, 0.15)   # 0 ~ 150ms

# 안전 지연 기본값
INVITE_DELAY_BASE   = 0.30   # 스레드 초대 사이
REACTION_DELAY_BASE = 0.25   # 이모지 추가 사이
DM_DELAY_BASE       = 1.00   # DM 발송 사이(보수적)
ACTION_DELAY_BASE   = 0.10   # 액션 묶음 사이 (여유 간격)

# 반응(이모지) 상한: 번호(최대 10) + ✅ + 💰 = 12
MAX_REACTIONS_PER_MESSAGE = 12

def with_jitter(base: float) -> float:
    lo, hi = DELAY_JITTER_RANGE
    return base + random.uniform(lo, hi)

async def pace(base: float):
    """기본 지연 + 지터"""
    await asyncio.sleep(with_jitter(base))

# ================== 유틸 ==================
async def safe_delete(msg, delay=delete_delay):
    if msg is None:
        return
    try:
        await asyncio.sleep(delay)
        await msg.delete()
    except discord.HTTPException as e:
        print(f"[WARN] 메시지 삭제 실패: {e}")
    except Exception as e:
        print(f"[WARN] 삭제 중 알 수 없는 오류: {e}")

# ================== Bot ==================
class MyBot(commands.Bot):
    async def setup_hook(self):
        # 백그라운드 작업 큐 워커(순차 처리)
        self.loop.create_task(background_worker())

        # 슬래시 명령 동기화
        try:
            synced = await self.tree.sync()
            print(f"✅ 슬래시 명령어 {len(synced)}개 동기화 완료!")
        except Exception as e:
            print(f"❌ 슬래시 명령어 동기화 실패: {e}")

bot = MyBot(command_prefix="!", intents=intents)

# ================== 백그라운드 워커 ==================
async def background_worker():
    """전역 백그라운드 큐를 1개 워커로 순차 처리"""
    while True:
        job_coro = await bg_queue.get()
        try:
            await job_coro
        except Exception as e:
            print(f"[ERROR] bg job 실패: {e}")
        finally:
            bg_queue.task_done()
            # 액션 사이 기본 간격
            await pace(ACTION_DELAY_BASE)

# ================== 백그라운드 작업(순차) ==================
async def background_finalize(message, item, author, mention_list, embed):
    """스레드 생성/초대 + 이모지 추가를 '전역 큐'에서 순차 처리"""
    try:
        # 1) 스레드 생성
        thread = await message.create_thread(name=f"{item} 분배", auto_archive_duration=60)
        await pace(ACTION_DELAY_BASE)

        # 2) 작성자 + 대상자 초대 (보수적으로 지연)
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

        # 3) 반응 버튼(이모지) 추가 (상한 & 지연)
        num_reactions = min(len(mention_list), len(emoji_list), MAX_REACTIONS_PER_MESSAGE)
        for i in range(num_reactions):
            await message.add_reaction(emoji_list[i])
            await pace(REACTION_DELAY_BASE)

        # ✅, 💰 추가(상한 고려)
        added = num_reactions
        if added < MAX_REACTIONS_PER_MESSAGE:
            await message.add_reaction(check_emoji)
            await pace(REACTION_DELAY_BASE)
            added += 1
        if added < MAX_REACTIONS_PER_MESSAGE:
            await message.add_reaction(sell_emoji)

    except Exception as e:
        print(f"[ERROR] background_finalize 실패: {e}")

async def background_notify_sale(guild_id, channel_id, message_id, creator_name, mentions, item):
    """판매완료 DM 전송을 전역 큐에서 순차 처리"""
    msg_link = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    for m in mentions:
        try:
            await m.send(f"👤 :{creator_name} 님의 분배 게시자에요.\n💰 `{item}` 아이템이 판매 완료되었어요!\n🔗 [바로가기]({msg_link})")
        except discord.Forbidden:
            # DM 차단 시는 채널에 경고 (가능하면 너무 시끄럽지 않게)
            pass
        await pace(DM_DELAY_BASE)

# ================== 게시판 글 즉시 표시 ==================
async def create_distribution(channel, author, item, mention_list):
    # 이모지 개수 상한
    safe_mentions = mention_list[:10]  # 숫자 이모지 10개까지만
    lines = [f"{emoji_list[i]} {m.mention}" for i, m in enumerate(safe_mentions)]

    now = now_kst()  # ✅ KST 기준
    date_str = now.strftime('%m/%d')
    time_str = now.strftime('%p %I:%M').replace('AM', '오전').replace('PM', '오후')

    # 1) 임베드 즉시 구성 (ID는 전송 후 알 수 있으므로 임시 제목)
    embed = discord.Embed(title="🍆 아이템 분배 안내", color=0x9146FF)
    summary = f"🎁 아이템명 : {item}\n📅 날짜 및 시간 : {date_str} {time_str}\n👤 생성자 : {author.mention}"
    embed.add_field(name="ℹ️ 기본 정보", value=summary, inline=False)
    embed.add_field(name="🎯 수령 대상자", value="\n".join(lines) if lines else "등록된 대상자가 없습니다.", inline=False)
    embed.add_field(name="📢 사용법", value="🔸 번호 이모지 누르면 수령 처리!\n✅ 모두 체크되면 완료게시판으로 이동!\n💰 누르면 판매완료 DM 전송!", inline=False)
    embed.add_field(name="💸 판매금액", value="미입력", inline=False)

    # 2) 임베드 먼저 전송 → 즉시 표시
    msg = await channel.send(embed=embed)

    # 3) 이제 msg.id를 제목에 반영
    embed.title = f"🍆 아이템 분배 안내 (ID: {msg.id})"
    await msg.edit(embed=embed)

    # 4) 상태 저장
    distribution_data[msg.id] = {
        "creator": author,
        "mentions": safe_mentions,
        "received": set(),    # ✅ 체크된 '번호 인덱스' 저장
        "message": msg,
        "embed": embed,
        "item": item,
        "datetime": now,      # ✅ KST
        "price": "미입력"
    }

    # 5) 느린 작업은 전역 큐로 순차 처리
    await bg_queue.put(background_finalize(msg, item, author, safe_mentions, embed))

# ================== 느낌표 명령어 ==================
@bot.command()
async def 분배(ctx, *, arg):
    await safe_delete(ctx.message)
    try:
        item, _ = map(str.strip, arg.split('/', 1))
        mention_list = ctx.message.mentions
        await create_distribution(ctx.channel, ctx.author, item, mention_list)
    except Exception as e:
        await safe_delete(await ctx.send(f"❌ 오류 발생: {e}"))

@bot.command()
async def 판매(ctx, message_id: int, *, content: str):
    await safe_delete(ctx.message)
    try:
        if message_id in distribution_data:
            msg_data = distribution_data[message_id]
            msg_data['price'] = content
            embed = msg_data['embed']
            embed.set_field_at(index=3, name="💸 판매금액", value=content, inline=False)
            await msg_data['message'].edit(embed=embed)
            await safe_delete(await ctx.send(f"💸 판매금액이 등록되었습니다: `{content}`"))
        else:
            await safe_delete(await ctx.send("❌ 해당 메시지를 찾을 수 없습니다."))
    except Exception as e:
        await safe_delete(await ctx.send(f"❌ 오류: {e}"))

@bot.command(name="ㅍ")
async def 판매_축약(ctx, message_id: int, *, content: str):
    await safe_delete(ctx.message)
    await 판매(ctx, message_id, content=content)

@bot.command()
async def 분배중(ctx):
    await safe_delete(ctx.message)
    # ✅ 본인 이름 옆에 ✅가 붙은 항목(누가 눌렀든)은 DM 제외
    await send_distribution_list(ctx.author, ctx.guild, ctx.channel, exclude_completed=True)

# ================== 슬래시 명령어 ==================
@bot.tree.command(name="분배", description="아이템 분배 등록")
@app_commands.describe(item="아이템 이름", 대상자="수령 대상자 멘션")
async def slash_분배(interaction: discord.Interaction, item: str, 대상자: str):
    await interaction.response.defer(ephemeral=True)
    mention_list = [m for m in interaction.channel.members if f"<@{m.id}>" in 대상자 or f"<@!{m.id}>" in 대상자]
    await create_distribution(interaction.channel, interaction.user, item, mention_list)
    await interaction.followup.send("✅ 분배 등록 완료!", ephemeral=True)

@bot.tree.command(name="분배중", description="내가 수령자에 포함된 분배 목록을 DM으로 받아보세요 (완료한 항목은 제외)")
async def slash_분배중(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await send_distribution_list(interaction.user, interaction.guild, interaction.channel, exclude_completed=True)
    await interaction.followup.send("📬 DM으로 전송했어요!", ephemeral=True)

# ================== 리액션 이벤트 ==================
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    await handle_reaction_event(reaction, user, is_add=True)

@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot:
        return
    await handle_reaction_event(reaction, user, is_add=False)

async def handle_reaction_event(reaction, user, is_add):
    msg_id = reaction.message.id
    if msg_id not in distribution_data:
        return

    data = distribution_data[msg_id]
    emoji = str(reaction.emoji)
    message = data["message"]
    embed = data["embed"]
    guild = message.guild
    완료채널 = guild.get_channel(완료_채널_ID)

    async def 종료처리():
        try:
            if 완료채널:
                await 완료채널.send(embed=embed)
            if hasattr(message, "thread") and message.thread:
                await message.thread.delete()
            await message.delete()
        except Exception as e:
            print(f"[ERROR] 종료 처리 중 오류: {e}")

    if emoji in emoji_list:
        index = emoji_list.index(emoji)
        if is_add:
            data["received"].add(index)
        else:
            data["received"].discard(index)

        # 임베드의 수령 대상자 표시 갱신 (✅ 토글)
        lines = []
        for i, m in enumerate(data["mentions"]):
            line = f"{emoji_list[i]} {m.mention}"
            if i in data["received"]:
                line += " ✅"
            lines.append(line)

        embed.set_field_at(1, name="🎯 수령 대상자", value="\n".join(lines) if lines else "등록된 대상자가 없습니다.", inline=False)
        await message.edit(embed=embed)

        # 모두 체크되면 자동 종료
        if is_add and len(data["received"]) == len(data["mentions"]):
            await safe_delete(await message.channel.send("✅ 모든 대상자 수령 완료. 분배 종료!"))
            await 종료처리()
            del distribution_data[msg_id]

    elif is_add and emoji == sell_emoji:
        await safe_delete(await message.channel.send("💰 판매 완료! DM을 전송해요."))
        creator = data["creator"].display_name
        # DM 전송도 전역 큐에서 순차 처리
        await bg_queue.put(background_notify_sale(
            guild.id, message.channel.id, message.id, creator, data["mentions"], data["item"]
        ))

    elif is_add and emoji == check_emoji:
        await safe_delete(await message.channel.send("✅ 강제 종료 처리가 완료되었습니다."))
        await 종료처리()
        del distribution_data[msg_id]

# ================== 분배 목록 DM (완료 제외 옵션) ==================
async def send_distribution_list(user, guild, channel, exclude_completed: bool = True):
    """
    exclude_completed=True:
      본인 이름 옆에 ✅가 붙어 있으면(본인/타인 누름 상관없이) DM 목록에서 제외.
    """
    found = []
    for msg_id, data in distribution_data.items():
        if user in data["mentions"]:
            # 본인의 멘션 인덱스
            try:
                idx = data["mentions"].index(user)
            except ValueError:
                continue

            # ✅ 이미 체크된 경우 DM 제외
            if exclude_completed and idx in data["received"]:
                continue

            dt = data["datetime"]  # 이미 KST
            date_str = dt.strftime('%m/%d')
            time_str = dt.strftime('%p %I:%M').replace('AM', '오전').replace('PM', '오후')
            author = data["creator"].display_name
            link = f"https://discord.com/channels/{guild.id}/{data['message'].channel.id}/{msg_id}"
            found.append(f"{data['item']} | 🕛 {date_str} ⏰ {time_str} 👤 {author}\n → [바로가기]({link})")

    if found:
        try:
            await user.send("\n".join([f"📄 {user.display_name} 님의 분배 목록 (완료 제외):"] + found))
        except discord.Forbidden:
            await channel.send(f"⚠️ {user.mention}님에게 DM을 보내지 못했습니다.", delete_after=5)
    else:
        await channel.send(f"🔍 {user.mention}님이 확인할 미완료 분배 항목이 없습니다.", delete_after=5)

# ================== 실행 ==================
bot.run(TOKEN)