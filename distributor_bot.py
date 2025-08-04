import discord
import os
import asyncio
from discord.ext import commands
from discord import app_commands
from datetime import datetime
from dotenv import load_dotenv
from keepalive import keep_alive

# ✅ 서버 유지
keep_alive()

# ✅ 환경변수 로드
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ✅ Intents 설정
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# ✅ 이모지 목록 및 상수 정의
emoji_list = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
check_emoji = '✅'
sell_emoji = '💰'
완료_채널_ID = 1399368173949550692

distribution_data = {}
delete_delay = 10
reaction_queue = []

# ✅ 안전한 메시지 삭제 함수
def enqueue(coro):
    reaction_queue.append(coro)

async def safe_delete(msg, delay=delete_delay):
    try:
        await asyncio.sleep(delay)
        await msg.delete()
    except discord.HTTPException as e:
        print(f"[WARN] 메시지 삭제 실패: {e}")
    except Exception as e:
        print(f"[WARN] 삭제 중 알 수 없는 오류: {e}")

async def reaction_worker():
    while True:
        if reaction_queue:
            coro = reaction_queue.pop(0)
            try:
                await coro
            except Exception as e:
                print(f"[ERROR] reaction 처리 중 오류: {e}")
        await asyncio.sleep(0.5)

bot.loop.create_task(reaction_worker())

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ 슬래시 명령어 {len(synced)}개 동기화 완료!")
    except Exception as e:
        print(f"❌ 슬래시 명령어 동기화 실패: {e}")

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
    await send_distribution_list(ctx.author, ctx.guild, ctx.channel)

@bot.tree.command(name="분배", description="아이템 분배 등록")
@app_commands.describe(item="아이템 이름", 대상자="수령 대상자 멘션")
async def slash_분배(interaction: discord.Interaction, item: str, 대상자: str):
    await interaction.response.defer(ephemeral=True)
    mention_list = [m for m in interaction.channel.members if f"<@{m.id}>" in 대상자 or f"<@!{m.id}>" in 대상자]
    await create_distribution(interaction.channel, interaction.user, item, mention_list)
    await interaction.followup.send("✅ 분배 등록 완료!", ephemeral=True)

@bot.tree.command(name="분배중", description="내가 수령자에 포함된 분배 목록을 DM으로 받아보세요")
async def slash_분배중(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await send_distribution_list(interaction.user, interaction.guild, interaction.channel)
    await interaction.followup.send("📬 DM으로 전송했어요!", ephemeral=True)

# ✅ on_reaction_add 이벤트도 reaction_queue 활용
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    async def handle():
        await handle_reaction_event(reaction, user, is_add=True)
    enqueue(handle())

@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot:
        return
    async def handle():
        await handle_reaction_event(reaction, user, is_add=False)
    enqueue(handle())

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

        lines = []
        for i, m in enumerate(data["mentions"]):
            line = f"{emoji_list[i]} {m.mention}"
            if i in data["received"]:
                line += " ✅"
            lines.append(line)

        embed.set_field_at(1, name="🎯 수령 대상자", value="\n".join(lines), inline=False)
        await message.edit(embed=embed)

        if is_add and len(data["received"]) == len(data["mentions"]):
            await safe_delete(await message.channel.send("✅ 모든 대상자 수령 완료. 분배 종료!"))
            await 종료처리()
            del distribution_data[msg_id]

    elif is_add and emoji == sell_emoji:
        await safe_delete(await message.channel.send("💰 판매 완료! DM을 전송해요."))
        creator = data["creator"].display_name
        for m in data["mentions"]:
            try:
                msg_link = f"https://discord.com/channels/{guild.id}/{message.channel.id}/{message.id}"
                await m.send(f"👤 :{creator} 님의 분배 게시자에요.\n💰 `{data['item']}` 아이템이 판매 완료되었어요!\n🔗 [바로가기]({msg_link})")
            except discord.Forbidden:
                await safe_delete(await message.channel.send(f"⚠️ {m.display_name}님에게 DM을 보내지 못했습니다."))

    elif is_add and emoji == check_emoji:
        await safe_delete(await message.channel.send("✅ 강제 종료 처리가 완료되었습니다."))
        await 종료처리()
        del distribution_data[msg_id]

async def create_distribution(channel, author, item, mention_list):
    lines = [f"{emoji_list[i]} {m.mention}" for i, m in enumerate(mention_list)]
    now = datetime.now()
    date_str = now.strftime('%m/%d')
    time_str = now.strftime('%p %I:%M').replace('AM', '오전').replace('PM', '오후')
    msg = await channel.send("분배 메시지 준비 중...")

    embed = discord.Embed(title=f"🍆 아이템 분배 안내 (ID: {msg.id})", color=0x9146FF)
    summary = f"🎁 아이템명 : {item}\n📅 날짜 및 시간 : {date_str} {time_str}\n👤 생성자 : {author.mention}"
    embed.add_field(name="ℹ️ 기본 정보", value=summary, inline=False)
    embed.add_field(name="🎯 수령 대상자", value="\n".join(lines), inline=False)
    embed.add_field(name="📢 사용법", value="🔸 번호 이모지 누르면 수령 처리!\n✅ 모든 이모지 누르면 완료게사판으로 슝~!\n💰 누르면 판매완료 DM 전송!", inline=False)
    embed.add_field(name="💸 판매금액", value="미입력", inline=False)

    await msg.edit(content="", embed=embed)
    thread = await msg.create_thread(name=f"{item} 분배", auto_archive_duration=60)
    await thread.add_user(author)
    for m in mention_list:
        await thread.add_user(m)

    for i in range(len(mention_list)):
        await msg.add_reaction(emoji_list[i])
        await asyncio.sleep(0.3)

    await msg.add_reaction(check_emoji)
    await asyncio.sleep(0.3)
    await msg.add_reaction(sell_emoji)
    await asyncio.sleep(0.3)

    distribution_data[msg.id] = {
        "creator": author,
        "mentions": mention_list,
        "received": set(),
        "message": msg,
        "embed": embed,
        "item": item,
        "datetime": now,
        "price": "미입력"
    }

async def send_distribution_list(user, guild, channel):
    found = []
    for msg_id, data in distribution_data.items():
        if user in data["mentions"]:
            dt = data["datetime"]
            date_str = dt.strftime('%m/%d')
            time_str = dt.strftime('%p %I:%M').replace('AM', '오전').replace('PM', '오후')
            author = data["creator"].display_name
            link = f"https://discord.com/channels/{guild.id}/{data['message'].channel.id}/{msg_id}"
            found.append(f"{data['item']} | 🕛 {date_str} ⏰ {time_str} 👤 {author}\n → [바로가기]({link})")

    if found:
        try:
            await user.send("\n".join([f"📄 {user.display_name} 님의 분배 목록:"] + found))
        except discord.Forbidden:
            await channel.send(f"⚠️ {user.mention}님에게 DM을 보내지 못했습니다.", delete_after=5)
    else:
        await channel.send(f"🔍 {user.mention}님이 포함된 분배 항목이 없습니다.", delete_after=5)

# ✅ 봇 실행
bot.run(TOKEN)