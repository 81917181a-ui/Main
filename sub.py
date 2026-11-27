# ==========================================
# sub.py (Modmail サポートチケット + ポイントシステム)
# ==========================================
import asyncio
import io
import json
import os
import random
import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

# --- 固定設定（サポートチケット） ---
INBOX_CATEGORY_ID = 1513901626610553043   # 問い合わせが入るカテゴリー
LOG_CHANNEL_ID = 1510042822533840936      # ログが送信されるチャンネル
RATING_CHANNEL_ID = 1510639675239432313   # ★評価と改善点が届くチャンネル
ADMIN_ROLE_ID = 1510405214811852900       # 運営・管理者ロールID

# --- ポイントシステム用設定 ---
POINT_DATABASE_CHANNEL_ID = 1527164312634920980
WORK_ROLE_ID = 1510021467155202057
ADMIN_ROLE_ID_POINTS = 1510405214811852900

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POINTS_FILE = os.path.join(BASE_DIR, "points.json")

points_data = {}


# ==========================================
# 💎 Discordチャンネルからポイントを自動復元・同期する関数
# ==========================================
async def sync_points_from_discord(bot: commands.Bot):
    global points_data
    print("【データベース】Discordのチャンネルからポイントデータの同期を開始します...")

    channel = bot.get_channel(POINT_DATABASE_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(POINT_DATABASE_CHANNEL_ID)
        except Exception as e:
            print(f"【エラー】同期対象のチャンネルが見つかりません: {e}")
            return

    temp_data = {}
    count = 0

    async for message in channel.history(limit=5000, oldest_first=True):
        if message.author.id != bot.user.id or not message.embeds:
            continue

        embed = message.embeds[0]
        if embed.title and "ポイント変動通知" in embed.title:
            target_field = None
            change_field = None
            reason_field = "システムによる操作"

            for field in embed.fields:
                if field.name == "対象者ID":
                    target_field = field.value
                elif field.name == "変動値":
                    change_field = field.value
                elif field.name in ["理由", "使用用途（理由）"]:
                    reason_field = field.value

            if target_field and change_field:
                user_id_str = "".join(filter(str.isdigit, target_field))
                try:
                    change_amount = int(change_field.replace(" pt", "").replace("+", "").replace(" ", ""))
                except ValueError:
                    continue

                if user_id_str not in temp_data:
                    temp_data[user_id_str] = {"points": 0, "logs": []}

                temp_data[user_id_str]["points"] += change_amount

                time_str = message.created_at.strftime("%Y-%m-%d %H:%M")
                sign = "+" if change_amount >= 0 else ""
                emoji = "🪙" if change_amount >= 0 else "💸"
                log_entry = f"[{time_str}] {emoji} {sign}{change_amount} pt ({reason_field})"
                temp_data[user_id_str]["logs"].append(log_entry)

                count += 1

    points_data = temp_data
    save_points()
    print(f"【データベース】同期完了！計 {count} 件のログから {len(points_data)} 人分のデータを復元しました。")


# ==========================================
# ポイントデータ管理関数（ファイル保存）
# ==========================================
def load_points():
    global points_data
    if os.path.exists(POINTS_FILE):
        try:
            with open(POINTS_FILE, "r", encoding="utf-8") as f:
                points_data = json.load(f)
                print("【システム】ローカルのポイントデータを読み込みました。")
                return
        except Exception as e:
            print(f"データ読み込み失敗: {e}")
    points_data = {}


def save_points():
    try:
        with open(POINTS_FILE, "w", encoding="utf-8") as f:
            json.dump(points_data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"データ保存失敗: {e}")


def get_user_data(user_id: str):
    if user_id not in points_data:
        points_data[user_id] = {"points": 0, "logs": []}
        save_points()
    return points_data[user_id]


def update_user_data(user_id: str, points: int, log_entry: str):
    if user_id not in points_data:
        points_data[user_id] = {"points": 0, "logs": []}
    points_data[user_id]["points"] = points
    points_data[user_id]["logs"].append(log_entry)
    save_points()


# ==========================================
# 🎫 サポートチケット (Modmail) 共通関数
# ==========================================
def get_ticket_user_id(topic: str):
    """チャンネルtopicから問い合わせユーザーIDを取得"""
    if not topic:
        return None
    m = re.search(r"User ID:\s*(\d+)", topic)
    return int(m.group(1)) if m else None


def get_ticket_staff_id(topic: str):
    """チャンネルtopicから担当スタッフIDを取得（未担当ならNone）"""
    if not topic:
        return None
    m = re.search(r"Staff ID:\s*(\d+)", topic)
    return int(m.group(1)) if m else None


def is_staff(member) -> bool:
    """運営ロール or 管理者権限を持っているか"""
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    return any(r.id == ADMIN_ROLE_ID for r in member.roles)


def attachments_text(message: discord.Message) -> str:
    return "\n".join(f"📎 {a.url}" for a in message.attachments)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ==========================================
# [DM用] 改善点・問題点の入力モーダル
# ==========================================
class FeedbackModal(discord.ui.Modal):
    def __init__(self, rating_stars: int, user_id: int):
        super().__init__(title="問い合わせへのフィードバック")
        self.rating_stars = rating_stars
        self.user_id = user_id

    feedback_text = discord.ui.TextInput(
        label="改善点・問題点があればご記入ください",
        style=discord.TextStyle.paragraph,
        placeholder="ここに入力してください（任意）",
        required=False,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        stars_str = "⭐" * self.rating_stars if self.rating_stars > 0 else "🖤 (星0)"
        embed = discord.Embed(
            title="📊 問い合わせ評価・フィードバック受信",
            color=discord.Color.purple(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="👤 評価ユーザー", value=f"<@{self.user_id}> (`{self.user_id}`)", inline=False)
        embed.add_field(name="⭐ 満足度評価", value=f"**{stars_str}** ({self.rating_stars}/5)", inline=False)
        embed.add_field(name="💬 改善点・問題点", value=self.feedback_text.value or "*記述なし*", inline=False)

        rating_channel = interaction.client.get_channel(RATING_CHANNEL_ID)
        if rating_channel:
            await rating_channel.send(embed=embed)
        await interaction.followup.send("ご協力ありがとうございました！", ephemeral=True)


# ==========================================
# [DM用] 星評価（0〜5）のボタン
# ==========================================
class StarRatingView(discord.ui.View):
    STYLES = [
        discord.ButtonStyle.secondary,
        discord.ButtonStyle.danger,
        discord.ButtonStyle.danger,
        discord.ButtonStyle.success,
        discord.ButtonStyle.success,
        discord.ButtonStyle.primary,
    ]

    def __init__(self, user_id: int):
        super().__init__(timeout=600)
        self.user_id = user_id
        for stars in range(6):
            button = discord.ui.Button(label=f"⭐{stars}", style=self.STYLES[stars])
            button.callback = self._make_callback(stars)
            self.add_item(button)

    def _make_callback(self, stars: int):
        async def callback(interaction: discord.Interaction):
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
            await interaction.response.send_modal(FeedbackModal(rating_stars=stars, user_id=self.user_id))
        return callback


# ==========================================
# スタッフ用：担当者登録ボタン（再起動後も動く永続View）
# ==========================================
class SupportClaimView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🙋‍♂️ 担当する", style=discord.ButtonStyle.primary, custom_id="modmail_claim_ticket")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ この操作は運営スタッフのみ実行できます。", ephemeral=True)
            return

        channel = interaction.channel
        user_id = get_ticket_user_id(channel.topic)
        if user_id is None:
            await interaction.response.send_message("❌ トピックからユーザーIDが読み取れません。", ephemeral=True)
            return
        if get_ticket_staff_id(channel.topic):
            await interaction.response.send_message("❌ このチケットは既に他のスタッフが担当しています。", ephemeral=True)
            return

        await interaction.response.defer()
        staff = interaction.user

        # 完了表示用に新しいViewを作る（永続Viewの共有インスタンスは書き換えない）
        done_view = SupportClaimView()
        done_view.claim.label = f"担当者: {staff.display_name}"
        done_view.claim.style = discord.ButtonStyle.success
        done_view.claim.disabled = True
        await interaction.message.edit(view=done_view)

        user_name = channel.name.split("-with-")[0]
        try:
            await channel.edit(
                name=f"{user_name}-with-{staff.name.lower()}"[:100],
                topic=f"User ID: {user_id} | Staff ID: {staff.id}",
            )
        except Exception as e:
            await channel.send(f"⚠️ チャンネル情報の更新に失敗: {e}")

        await channel.send(f"🤝 {staff.mention} が担当者になりました。通常発言がユーザーに転送されます。")

        try:
            user = await interaction.client.fetch_user(user_id)
            embed = discord.Embed(
                description=f"💁‍♂️ あなたの問い合わせの担当者が **{staff.display_name}** に決定しました。\n用件をそのままお話しください。",
                color=discord.Color.green(),
            )
            await user.send(embed=embed)
        except Exception as e:
            await channel.send(f"⚠️ ユーザーへのDM通知に失敗: {e}")


# ==========================================
# 🎫 サポートチケット Cog
# ==========================================
class Modmail(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = True
        self._locks = {}  # ユーザーごとのチケット重複作成防止

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def _get_category(self):
        category = self.bot.get_channel(INBOX_CATEGORY_ID)
        if category is None:
            try:
                category = await self.bot.fetch_channel(INBOX_CATEGORY_ID)
            except Exception:
                return None
        return category

    @staticmethod
    def _find_ticket(category: discord.CategoryChannel, user_id: int):
        for ch in category.text_channels:
            if get_ticket_user_id(ch.topic) == user_id:
                return ch
        return None

    # ---------- メッセージ受信 ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # 1. ユーザー ➡ BotへのDM
        if isinstance(message.channel, discord.DMChannel):
            await self._handle_dm(message)

        # 2. スタッフ ➡ ユーザーのDMへ転送
        elif isinstance(message.channel, discord.TextChannel) and message.channel.category_id == INBOX_CATEGORY_ID:
            await self._handle_staff_message(message)

    async def _handle_dm(self, message: discord.Message):
        if not self.enabled:
            await message.channel.send("❌ 現在サポート機能は一時停止中です。再開までお待ちください。")
            return

        category = await self._get_category()
        if category is None:
            await message.channel.send("❌ 現在問い合わせを受け付けできません。時間をおいて再度お試しください。")
            return
        guild = category.guild

        body = message.content or ""
        files = attachments_text(message)
        if not body and not files:
            return

        async with self._lock(message.author.id):
            ticket = self._find_ticket(category, message.author.id)

            if ticket is None:
                staff_role = guild.get_role(ADMIN_ROLE_ID)
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                }
                if staff_role:
                    overwrites[staff_role] = discord.PermissionOverwrite(
                        read_messages=True, send_messages=True, manage_messages=True
                    )

                try:
                    ticket = await guild.create_text_channel(
                        name=message.author.name.lower(),
                        category=category,
                        topic=f"User ID: {message.author.id}",
                        overwrites=overwrites,
                    )
                except Exception as e:
                    await message.channel.send(f"❌ チケットの作成に失敗しました。運営へご連絡ください。\n`{e}`")
                    return

                welcome = discord.Embed(
                    title="📩 問い合わせを受け付けました",
                    description="サーバー運営チームにメッセージが転送されました。\n担当者が決定するまでしばらくお待ちください。",
                    color=discord.Color.blue(),
                )
                await message.channel.send(embed=welcome)

                member = guild.get_member(message.author.id)
                roles_str = (
                    ", ".join(r.mention for r in member.roles if r != guild.default_role) if member else "取得不可"
                )
                join_at = member.joined_at.strftime("%Y/%m/%d %H:%M") if member and member.joined_at else "不明"

                info = discord.Embed(title="📩 新規問い合わせチケット", color=discord.Color.orange())
                info.add_field(name="👤 ユーザー名", value=f"{message.author.mention} ({message.author})", inline=True)
                info.add_field(name="🆔 ユーザーID", value=f"`{message.author.id}`", inline=True)
                info.add_field(name="📅 サーバー参加日", value=join_at, inline=False)
                info.add_field(name="🛡️ 所持ロール", value=truncate(roles_str or "なし", 1024), inline=False)
                info.add_field(
                    name="💬 メッセージ",
                    value=truncate((body + ("\n" + files if files else "")).strip(), 1024),
                    inline=False,
                )
                await ticket.send(embed=info, view=SupportClaimView())
                await message.add_reaction("✅")
                return

        # 既存チケットへ転送
        embed = discord.Embed(
            description=truncate((body + ("\n" + files if files else "")).strip(), 4000),
            color=discord.Color.green(),
        )
        embed.set_author(name=f"{message.author} (DM)", icon_url=message.author.display_avatar.url)
        await ticket.send(embed=embed)
        await message.add_reaction("✅")

    async def _handle_staff_message(self, message: discord.Message):
        content = message.content or ""
        # スラッシュ・コマンドは転送しない。「//」始まりはスタッフ間の内部メモ扱い
        if content.startswith(("/", "!")):
            return
        user_id = get_ticket_user_id(message.channel.topic)
        if user_id is None:
            return
        if not content and not message.attachments:
            return

        try:
            user = await self.bot.fetch_user(user_id)
        except Exception:
            await message.channel.send("❌ ユーザー情報を取得できませんでした。")
            return

        text = (content + ("\n" + attachments_text(message) if message.attachments else "")).strip()
        embed = discord.Embed(description=truncate(text, 4000), color=discord.Color.blue())
        embed.set_author(
            name=f"ダイヤ作成所（{message.author.display_name}）",
            icon_url=message.guild.icon.url if message.guild.icon else None,
        )

        try:
            await user.send(embed=embed)
            await message.add_reaction("✈️")
        except discord.Forbidden:
            await message.channel.send("❌ ユーザーのDMが閉じられているため転送できませんでした。")

    # ---------- 一時停止 / 再開 ----------
    @commands.command(name="supportstop")
    @commands.has_permissions(administrator=True)
    async def support_stop(self, ctx: commands.Context):
        self.enabled = False
        await ctx.send("🛑 サポート機能（Modmail）を一時停止しました。")

    @commands.command(name="supportstart")
    @commands.has_permissions(administrator=True)
    async def support_start(self, ctx: commands.Context):
        self.enabled = True
        await ctx.send("▶️ サポート機能（Modmail）を再開しました。")

    # ---------- /close ----------
    @app_commands.command(name="close", description="チケットをクローズします")
    @app_commands.describe(reason="クローズする理由（任意）")
    async def close_ticket(self, interaction: discord.Interaction, reason: str = "理由なし"):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.category_id != INBOX_CATEGORY_ID:
            await interaction.response.send_message("❌ このチャンネルでは実行できません。", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ この操作は運営スタッフのみ実行できます。", ephemeral=True)
            return
        user_id = get_ticket_user_id(channel.topic)
        if user_id is None:
            await interaction.response.send_message("❌ トピックからユーザーIDが読み取れません。", ephemeral=True)
            return

        await interaction.response.send_message("🔒 クローズ処理中...")

        try:
            user = await self.bot.fetch_user(user_id)
        except Exception:
            user = None

        # 会話ログ作成（最大500件）
        log_lines = []
        async for msg in channel.history(limit=500, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
            if msg.embeds:
                for emb in msg.embeds:
                    author_name = emb.author.name if emb.author else "システム"
                    desc = emb.description or ""
                    if desc:
                        log_lines.append(f"[{ts}] {author_name}: {desc}")
            elif msg.content and not msg.author.bot:
                log_lines.append(f"[{ts}] {msg.author.name}（スタッフ内部）: {msg.content}")
        full_log_text = "\n".join(log_lines)

        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title=f"🔒 チケットクローズログ: {channel.name}",
                description=(
                    f"**対象ユーザー:** {user.mention if user else channel.name}\n"
                    f"**クローズ実行者:** {interaction.user.mention}\n"
                    f"**理由:** {reason}"
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(),
            )
            if len(full_log_text) > 900 or len(full_log_text) == 0:
                file = discord.File(io.BytesIO(full_log_text.encode("utf-8")), filename=f"log-{channel.name}.txt")
                await log_channel.send(embed=log_embed, file=file)
            else:
                log_embed.add_field(name="📜 やり取り内容", value=f"```\n{full_log_text}\n```", inline=False)
                await log_channel.send(embed=log_embed)

        if user:
            try:
                end_embed = discord.Embed(
                    title="🔒 問い合わせクローズ",
                    description=f"運営スタッフによりチケットが閉じられました。\n**理由:** {reason}",
                    color=discord.Color.red(),
                )
                await user.send(embed=end_embed)

                rating_embed = discord.Embed(
                    title="📝 問い合わせ評価のお願い",
                    description="今回のサポート対応はいかがでしたでしょうか？\n下のボタンから **⭐0 〜 ⭐5** の評価をお選びください。",
                    color=discord.Color.gold(),
                )
                await user.send(embed=rating_embed, view=StarRatingView(user_id=user.id))
            except Exception:
                pass  # DM拒否など

        await asyncio.sleep(2)
        await channel.delete(reason=f"チケットクローズ: {reason}")

    # ---------- /change_staff ----------
    @app_commands.command(name="change_staff", description="チケットの担当スタッフをリセットして交代します")
    async def change_staff(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.category_id != INBOX_CATEGORY_ID:
            await interaction.response.send_message("❌ このチャンネルでは実行できません。", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ この操作は運営スタッフのみ実行できます。", ephemeral=True)
            return
        user_id = get_ticket_user_id(channel.topic)
        if user_id is None:
            await interaction.response.send_message("❌ トピックからユーザーIDが読み取れません。", ephemeral=True)
            return

        await interaction.response.defer()
        user_name = channel.name.split("-with-")[0]
        await channel.edit(name=user_name[:100], topic=f"User ID: {user_id}")
        reset_embed = discord.Embed(
            description="🔄 担当者がリセットされました。下のボタンを押して担当を交代してください。",
            color=discord.Color.yellow(),
        )
        await interaction.followup.send(embed=reset_embed, view=SupportClaimView())


# ==========================================
# 🪙 ポイントシステム スラッシュコマンド
# ==========================================
def setup_points_commands(bot: commands.Bot):
    load_points()

    @bot.tree.command(name="work", description="毎日の仕事をこなしてポイントを獲得します")
    @app_commands.checks.cooldown(1, 28800, key=lambda i: i.user.id)
    async def work_command(interaction: discord.Interaction):
        if not any(role.id == WORK_ROLE_ID for role in interaction.user.roles):
            embed = discord.Embed(description="❌ このコマンドを実行する権限（指定ロール）がありません。", color=discord.Color.red())
            await interaction.response.send_message(embed=embed, ephemeral=False)
            return

        if random.random() < 0.70:
            earned = random.randint(200, 300)
        else:
            earned = random.choice([random.randint(50, 199), random.randint(301, 400)])

        user_id_str = str(interaction.user.id)
        user_data = get_user_data(user_id_str)
        new_points = user_data["points"] + earned

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        log_entry = f"[{now_str}] 🪙 +{earned} pt (お仕事報酬)"

        update_user_data(user_id_str, new_points, log_entry)

        embed = discord.Embed(
            title="💼 お仕事完了！",
            description=f"{interaction.user.mention}さんが仕事を完了しました。\n\n獲得: **{earned}** ポイント 🪙",
            color=discord.Color.green(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="現在の総保有", value=f"`{new_points} pt`")
        await interaction.response.send_message(embed=embed, ephemeral=False)

        log_channel = interaction.client.get_channel(POINT_DATABASE_CHANNEL_ID)
        if log_channel:
            noti_embed = discord.Embed(title="📥 ポイント変動通知 (お仕事)", color=discord.Color.green(), timestamp=datetime.now())
            noti_embed.add_field(name="対象者", value=interaction.user.mention, inline=True)
            noti_embed.add_field(name="対象者ID", value=f"`{interaction.user.id}`", inline=True)
            noti_embed.add_field(name="変動値", value=f"+{earned} pt", inline=True)
            noti_embed.add_field(name="理由", value="お仕事報酬", inline=False)
            await log_channel.send(embed=noti_embed)

    @work_command.error
    async def work_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            total_seconds = int(error.retry_after)
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            time_str = f"**{hours}時間** " if hours > 0 else ""
            time_str += f"**{minutes}分**"

            embed = discord.Embed(
                title="⏳ まだお仕事はできません",
                description=f"お仕事のやりすぎです！次のお仕事まであと {time_str} お待ちください。",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="points", description="指定したユーザー（または自分）の保有ポイントを確認します")
    @app_commands.describe(user="ポイントを確認したいユーザーを指定（未指定で自分）")
    async def points_command(interaction: discord.Interaction, user: discord.User = None):
        target_user = user or interaction.user
        user_data = get_user_data(str(target_user.id))

        embed = discord.Embed(
            title="🪙 ポイント確認",
            description=f"{target_user.mention} さんの現在の残高です。",
            color=discord.Color.gold(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="ポイント残高", value=f"**{user_data['points']}** pt", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @bot.tree.command(name="pointlog", description="ポイントの利用履歴（通帳）を最大100件確認します")
    @app_commands.describe(user="履歴を確認したいユーザーを指定（未指定で自分）")
    async def pointlog_command(interaction: discord.Interaction, user: discord.User = None):
        target_user = user or interaction.user
        user_data = get_user_data(str(target_user.id))

        embed = discord.Embed(
            title="📜 ポイント利用明細（通帳）",
            description=f"{target_user.mention} さんの過去の履歴です。",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )

        logs = user_data.get("logs", [])
        if not logs:
            embed.description += "\n\n*履歴はまだありません。*"
            await interaction.response.send_message(embed=embed, ephemeral=False)
            return

        recent_logs = list(reversed(logs))[:100]
        full_log_text = "\n".join(recent_logs)

        if len(full_log_text) > 850:
            embed.description += "\n\n📋 履歴数が多いため、テキストファイルを作成して添付しました。"
            with io.StringIO(full_log_text) as f:
                file = discord.File(f, filename=f"passbook-{target_user.name}.txt")
                await interaction.response.send_message(embed=embed, file=file, ephemeral=False)
        else:
            embed.add_field(name="直近の履歴（最大100件）", value=f"```\n{full_log_text}\n```", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=False)

    @bot.tree.command(name="rankings", description="ポイントの所持数ランキング上位10名を表示します")
    async def rankings_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        filtered_users = [
            (user_id, data) for user_id, data in points_data.items()
            if data.get("points", 0) >= 1
        ]

        if not filtered_users:
            embed = discord.Embed(
                title="🏆 ポイントランキング",
                description="現在、1ポイント以上を所持しているユーザーはいません。",
                color=discord.Color.gold(),
            )
            await interaction.followup.send(embed=embed)
            return

        sorted_users = sorted(filtered_users, key=lambda item: item[1].get("points", 0), reverse=True)
        top_10 = sorted_users[:10]

        ranking_lines = []
        medals = ["🥇", "🥈", "🥉"]

        for index, (user_id_str, data) in enumerate(top_10, start=1):
            pts = data.get("points", 0)
            rank_display = medals[index - 1] if index <= 3 else f"**#{index}**"

            user_name = "不明なユーザー"
            try:
                user_id = int(user_id_str)
                user = interaction.client.get_user(user_id)
                if not user:
                    user = await interaction.client.fetch_user(user_id)
                if user:
                    user_name = discord.utils.escape_markdown(user.display_name)
            except Exception:
                user_name = f"ユーザー({user_id_str})"

            ranking_lines.append(f"{rank_display} **{user_name}**: `{pts:,} pt`")

        embed = discord.Embed(
            title="🏆 ポイント所持ランキング Top 10",
            description="\n".join(ranking_lines),
            color=discord.Color.gold(),
            timestamp=datetime.now(),
        )
        embed.set_footer(text=f"対象ユーザー数: {len(filtered_users)}名")

        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="give_points", description="【管理者専用】他人のポイントを増やします")
    @app_commands.describe(user="付与する対象のユーザー", amount="増やすポイント数", reason="付与する理由・説明")
    async def give_points_command(interaction: discord.Interaction, user: discord.User, amount: int, reason: str):
        if not any(role.id == ADMIN_ROLE_ID_POINTS for role in interaction.user.roles):
            embed = discord.Embed(description="❌ このコマンドを実行する権限がありません。", color=discord.Color.red())
            await interaction.response.send_message(embed=embed, ephemeral=False)
            return

        user_id_str = str(user.id)
        user_data = get_user_data(user_id_str)
        new_points = user_data["points"] + amount

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        log_entry = f"[{now_str}] 🪙 +{amount} pt (付与: {reason})"

        update_user_data(user_id_str, new_points, log_entry)

        res_embed = discord.Embed(
            title="✅ ポイント付与完了",
            description=f"{user.mention} に **{amount}** ポイントを付与しました。\n理由: {reason}",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=res_embed, ephemeral=False)

        log_channel = interaction.client.get_channel(POINT_DATABASE_CHANNEL_ID)
        if log_channel:
            noti_embed = discord.Embed(title="📥 ポイント変動通知 (付与)", color=discord.Color.green(), timestamp=datetime.now())
            noti_embed.add_field(name="対象者", value=user.mention, inline=True)
            noti_embed.add_field(name="対象者ID", value=f"`{user.id}`", inline=True)
            noti_embed.add_field(name="変動値", value=f"+{amount} pt", inline=True)
            noti_embed.add_field(name="理由", value=reason, inline=False)
            await log_channel.send(embed=noti_embed)

    @bot.tree.command(name="take_points", description="【管理者専用】他人のポイントを消費・減算します")
    @app_commands.describe(user="消費させる対象のユーザー", amount="減らすポイント数", reason="消費する理由・目的")
    async def take_points_command(interaction: discord.Interaction, user: discord.User, amount: int, reason: str):
        if not any(role.id == ADMIN_ROLE_ID_POINTS for role in interaction.user.roles):
            embed = discord.Embed(description="❌ このコマンドを実行する権限がありません。", color=discord.Color.red())
            await interaction.response.send_message(embed=embed, ephemeral=False)
            return

        user_id_str = str(user.id)
        user_data = get_user_data(user_id_str)
        new_points = user_data["points"] - amount

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        log_entry = f"[{now_str}] 💸 -{amount} pt (消費: {reason})"

        update_user_data(user_id_str, new_points, log_entry)

        res_embed = discord.Embed(
            title="⚠️ ポイント消費",
            description=f"{user.mention} のポイントを **{amount}** 消費しました。\n目的・理由: {reason}",
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=res_embed, ephemeral=False)

        log_channel = interaction.client.get_channel(POINT_DATABASE_CHANNEL_ID)
        if log_channel:
            noti_embed = discord.Embed(title="📤 ポイント変動通知 (消費)", color=discord.Color.red(), timestamp=datetime.now())
            noti_embed.add_field(name="対象者", value=user.mention, inline=True)
            noti_embed.add_field(name="対象者ID", value=f"`{user.id}`", inline=True)
            noti_embed.add_field(name="変動値", value=f"-{amount} pt", inline=True)
            noti_embed.add_field(name="使用用途（理由）", value=reason, inline=False)
            await log_channel.send(embed=noti_embed)
