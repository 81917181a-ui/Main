import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import asyncio
import io
import datetime
import re
import difflib
import logging

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    firebase_admin = None
    firestore = None

logger = logging.getLogger(__name__)

# ==========================================
# 永続化用チャンネル・メッセージ・ロール管理
# ==========================================
CONFIG_CHANNEL_ID = 1526289865719943329   # 設定保存用チャンネルID
LOG_CHANNEL_ID = 1510042822533840936     # ログ送信先チャンネルID
ADMIN_ROLE_ID = 1510405214811852900      # 基準となる管理者ロールID
PANEL_AUTO_SEND_CHANNEL_ID = 1531834782881808566  # 再起動時自動送信・更新先チャンネルID

config_message_id = None

# ==========================================
# 【追加】各種申請BOT機能用 設定値
# ==========================================
TICKET_CATEGORY_ID = 1510021468074016892      # 申請チケット作成先カテゴリーID
LEAVE_ROLE_NAME = "休職中"                     # 休職承認時に付与するロール名
BASE_MEMBER_ROLE_NAME = "メンバー（一般）"       # 退職処理でこのロールより上を全剥奪する基準ロール名
JST = datetime.timezone(datetime.timedelta(hours=9))
APPLICATION_TYPES = ["休職申請", "転属申請", "兼務申請", "退職申請"]
FIRESTORE_COLLECTION = "hr_leave_status"       # 休職者情報を保存するFirestoreコレクション名
FIREBASE_CREDENTIAL_PATH = "serviceAccountKey.json"  # ローカル用サービスアカウントキーのパス（環境に合わせて変更）

# ==========================================
# 【追加】Firebase初期化（Render環境変数 / ローカルファイルの両対応）
# ==========================================
db = None


def init_firebase():
    """Firebaseを初期化する。
    1) Render等の環境変数 FIREBASE_CREDENTIALS_JSON にサービスアカウントのJSON文字列が
       設定されていればそれを使用する（本番運用向け・秘密鍵をリポジトリに置かずに済む）。
    2) 環境変数が無ければ、ローカルのサービスアカウントキーファイル
       （環境変数 FIREBASE_CREDENTIALS_PATH、未設定時は FIREBASE_CREDENTIAL_PATH）を
       フォールバックとして使用する（ローカル開発向け）。
    どちらも無い場合はFirebase機能を無効化し、以降 get_firestore_client() は None を返す。"""
    global db

    if firebase_admin is None:
        logger.warning("Firebase: firebase-admin がインストールされていません。Firebase機能は無効化されます。")
        db = None
        return

    try:
        if firebase_admin._apps:
            # 既にアプリが初期化済み（再呼び出し等）の場合はクライアントのみ取得する
            db = firestore.client()
            return

        # Render環境変数からJSON文字列を取得
        env_json = os.getenv("FIREBASE_CREDENTIALS_JSON")

        if env_json:
            cred_dict = json.loads(env_json)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            logger.info("Firebase: Render環境変数からの初期化に成功しました。")
            return

        # ローカル環境用フォールバック
        local_key_path = os.getenv("FIREBASE_CREDENTIALS_PATH", FIREBASE_CREDENTIAL_PATH)
        if os.path.exists(local_key_path):
            cred = credentials.Certificate(local_key_path)
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            logger.info(f"Firebase: ローカルファイル({local_key_path})からの初期化に成功しました。")
            return

        logger.warning("Firebase: 認証情報が見つかりません。Firebase機能は無効化されます。")
        db = None
    except Exception as e:
        logger.error(f"Firebaseの初期化中にエラーが発生しました: {e}")
        db = None


init_firebase()


def get_firestore_client():
    """Firestoreクライアントを取得する（既存呼び出し箇所との互換用ラッパー）。
    未初期化、もしくは初回の初期化に失敗していた場合は再度初期化を試みる。"""
    global db
    if db is not None:
        return db
    init_firebase()
    return db


async def get_hr_log_channel(bot: commands.Bot):
    """各種申請の承認・不許可ログ送信先チャンネルを取得する（既存の LOG_CHANNEL_ID を流用）"""
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            print(f"❌ [HR] ログチャンネルの取得に失敗しました: {e}")
            return None
    return channel


def sanitize_channel_name(name: str) -> str:
    """Discordのチャンネル名として使える形式に整形する"""
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9\-_ぁ-んァ-ヶ一-龠々ー]", "", name)
    name = re.sub(r"\s+", "-", name)
    return name[:90] if name else "application"


def find_role_by_name(guild: discord.Guild, name: str):
    """完全一致→大文字小文字無視→部分一致→表記ゆれ（類似名）の順でロールを検索する。
    申請フォームに入力されたロール名が正確でなくても、近い名前のロールを見つけられるようにする。"""
    if not name:
        return None
    name = name.strip()
    if not name:
        return None

    # 1) 完全一致
    role = discord.utils.get(guild.roles, name=name)
    if role:
        return role

    # 2) 大文字小文字を無視した完全一致
    lower_name = name.lower()
    for r in guild.roles:
        if r.name.lower() == lower_name:
            return r

    # 3) 部分一致（どちらかがどちらかを含む）。複数ヒットした場合は名前が短い（=より近い）ものを優先
    partial_matches = [r for r in guild.roles if lower_name in r.name.lower() or r.name.lower() in lower_name]
    if partial_matches:
        partial_matches.sort(key=lambda r: len(r.name))
        return partial_matches[0]

    # 4) 表記ゆれ・誤字対応（類似度の高いロール名を検索）
    candidates = {r.name: r for r in guild.roles}
    close = difflib.get_close_matches(name, candidates.keys(), n=1, cutoff=0.6)
    if close:
        return candidates[close[0]]

    return None


def parse_ticket_footer(embed: discord.Embed):
    """チケットEmbedのフッターから UserID と 申請種別 を復元する"""
    text = embed.footer.text if embed.footer else ""
    result = {}
    for part in (text or "").split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            result[k.strip()] = v.strip()
    return result.get("UserID"), result.get("Type")


def get_field_value(embed: discord.Embed, label: str):
    for f in embed.fields:
        if f.name == label:
            return f.value
    return None


def parse_period(period_str: str):
    """『YYYY/MM/DD〜YYYY/MM/DD』形式の文字列を (開始日, 終了日) に分割する"""
    if not period_str:
        return None, None
    for sep in ["〜", "～", "~", "-"]:
        if sep in period_str:
            parts = period_str.split(sep, 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    return None, None

# 初期設定データ構造
quiz_config = {
    "admin_channel_id": None,
    "admin_message_id": None,
    "departments": {
        "ダイヤ作成部": {
            "is_open": True,
            "questions": [
                {"id": 1, "question": "志望動機・自己PRを教えてください。"},
                {"id": 2, "question": "得意なことやアピールしたい活動実績を教えてください。"}
            ]
        }
    }
}

def is_admin_role_or_higher(user: discord.Member) -> bool:
    """指定ロール(ADMIN_ROLE_ID)か、それより上位の位置にあるロールを持っているか判定"""
    if not isinstance(user, discord.Member):
        return False
     
    if user.guild.owner_id == user.id:
        return True

    target_role = user.guild.get_role(ADMIN_ROLE_ID)
    if not target_role:
        return user.guild_permissions.administrator

    return user.top_role.position >= target_role.position

async def get_config_channel(bot: commands.Bot):
    """設定保存用チャンネルを取得する"""
    channel = bot.get_channel(CONFIG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CONFIG_CHANNEL_ID)
        except Exception as e:
            print(f"❌ [Quiz] 設定保存用チャンネルの取得に失敗しました: {e}")
            return None
    return channel

async def save_config_to_discord(bot: commands.Bot):
    """設定データを指定のチャンネルに自動保存・更新する"""
    global config_message_id
    channel = await get_config_channel(bot)
    if not channel:
        return

    content = f"```json\n{json.dumps(quiz_config, ensure_ascii=False, indent=2)}\n```"
    try:
        if config_message_id:
            try:
                msg = await channel.fetch_message(config_message_id)
                await msg.edit(content=content)
                return
            except discord.NotFound:
                pass
         
        msg = await channel.send(content=content)
        config_message_id = msg.id
    except Exception as e:
        print(f"❌ [Quiz] 設定保存エラー: {e}")

async def load_config_from_discord(bot: commands.Bot):
    """起動時にDiscordから設定データを読み込む"""
    global config_message_id, quiz_config
    channel = await get_config_channel(bot)
    if not channel:
        return

    try:
        async for msg in channel.history(limit=20):
            if msg.author.id == bot.user.id and msg.content.startswith("```json"):
                json_str = msg.content.strip("`").replace("json\n", "").strip()
                loaded_data = json.loads(json_str)
                 
                if isinstance(loaded_data, dict) and "departments" in loaded_data:
                    quiz_config.clear()
                    quiz_config.update(loaded_data)
                    config_message_id = msg.id
                    return
    except Exception as e:
        print(f"❌ [Quiz] 設定復旧エラー: {e}")

def generate_admin_embed():
    embed = discord.Embed(
        title="⚙️ 自己推薦 システム管理者パネル",
        color=discord.Color.gold()
    )
     
    dept_text = "【現在の部署一覧】\n"
    departments = quiz_config.get("departments", {})
     
    if not departments:
        dept_text += "現在登録されている部署はありません。\n"
    else:
        for dept, data in departments.items():
            status = "🟢" if data.get("is_open", True) else "🔴"
            q_count = len(data.get("questions", []))
            dept_text += f"・{dept}: {status} (質問{q_count}個)\n"
             
    dept_text += "\n下のボタンから部署の追加・削除・質問設定・ON/OFF切替が行えます。"
    embed.description = dept_text
    return embed

async def execute_channel_close(channel: discord.TextChannel, user: discord.abc.User, bot: commands.Bot):
    """チャンネル削除処理およびログ保存の共通関数"""
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            print(f"❌ ログ送信チャンネルの取得に失敗しました: {e}")

    messages = []
    async for msg in channel.history(limit=500, oldest_first=True):
        timestamp = msg.created_at.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        content = msg.content if msg.content else "[埋め込み/メディアメッセージ]"
        messages.append(f"[{timestamp}] {msg.author.display_name} ({msg.author.id}): {content}")

    history_text = "\n".join(messages)
    file_data = io.BytesIO(history_text.encode("utf-8"))
    discord_file = discord.File(file_data, filename=f"history-{channel.name}.txt")

    log_embed = discord.Embed(
        title="🔒 応募チャンネルクローズドログ",
        description=f"**対象チャンネル:** `{channel.name}`\n**実行者:** {user.mention} (`{user.id}`)",
        color=discord.Color.red()
    )

    if log_channel:
        await log_channel.send(embed=log_embed, file=discord_file)

    await asyncio.sleep(2)
    await channel.delete(reason="応募チャンネル閉鎖のため")

async def start_quiz_session(channel: discord.TextChannel, applicant: discord.Member, bot: commands.Bot, dept_name: str, questions: list):
    answers = []

    def check(m: discord.Message):
        return m.author.id == applicant.id and m.channel.id == channel.id

    for q in questions:
        embed = discord.Embed(
            title=f"❓ 質問 {q['id']} / {len(questions)}",
            description=q["question"],
            color=discord.Color.blue()
        )
        embed.set_footer(text="30分以内にこのチャンネルにメッセージを送信して回答してください。")
        await channel.send(embed=embed)

        try:
            msg = await bot.wait_for("message", check=check, timeout=1800.0)
            answers.append({
                "question": q["question"],
                "answer": msg.content
            })
            await channel.send("✅ 回答を受け付けました。")
            await asyncio.sleep(1)
        except asyncio.TimeoutError:
            timeout_embed = discord.Embed(
                title="⏱️ タイムアウト",
                description="30分間回答がなかったため、質問セッションを終了しました。\n再度やり直す場合は `!close` でチャンネルをクローズしてください。",
                color=discord.Color.red()
            )
            await channel.send(embed=timeout_embed)
            return

    result_embed = discord.Embed(
        title="📄 応募回答が提出されました",
        description=f"**応募部署: {dept_name}**\nご回答ありがとうございました！審査完了までお待ちください。",
        color=discord.Color.green()
    )
    result_embed.set_author(name=f"{applicant.display_name} ({applicant.name})", icon_url=applicant.display_avatar.url)

    for idx, item in enumerate(answers, 1):
        result_embed.add_field(
            name=f"問{idx}. {item['question']}",
            value=item["answer"],
            inline=False
        )
    result_embed.set_footer(text=f"User ID: {applicant.id}")

    await channel.send(embed=result_embed)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            pass

    if log_channel:
        await log_channel.send(embed=result_embed)

class ReadyCheckView(discord.ui.View):
    def __init__(self, applicant: discord.Member = None, dept_name: str = "", questions: list = None):
        super().__init__(timeout=None)
        self.applicant = applicant
        self.dept_name = dept_name
        self.questions = questions or []

    @discord.ui.button(label="はい (開始する)", style=discord.ButtonStyle.success, custom_id="quiz_ready_yes_persistent")
    async def ready_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if self.applicant and interaction.user.id != self.applicant.id:
            await interaction.followup.send("⚠️ 応募者本人しか操作できません。", ephemeral=True)
            return

        button.disabled = True
        try:
            await interaction.edit_original_response(content="👍 準備完了ですね！それでは質問を開始します。", view=None)
        except Exception:
            pass
         
        applicant = self.applicant or interaction.user
        questions = self.questions
        if not questions and self.dept_name:
            questions = quiz_config.get("departments", {}).get(self.dept_name, {}).get("questions", [])

        await start_quiz_session(interaction.channel, applicant, interaction.client, self.dept_name, questions)

class QuizUserPanelSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for dept, data in quiz_config.get("departments", {}).items():
            desc = "🟢 受付中" if data.get("is_open", True) else "🔴 停止中"
            options.append(discord.SelectOption(label=dept, description=desc, value=dept))
             
        if not options:
            options.append(discord.SelectOption(label="現在募集中の部署はありません", value="none"))
             
        super().__init__(placeholder="応募する部署を選択してください...", min_values=1, max_values=1, options=options, custom_id="quiz_user_select_persistent")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        dept_name = self.values[0]
        if dept_name == "none":
            await interaction.followup.send("現在応募できる部署がありません。", ephemeral=True)
            return

        dept_data = quiz_config.get("departments", {}).get(dept_name)
        if not dept_data or not dept_data.get("is_open", True):
            await interaction.followup.send(f"🚫 「{dept_name}」は存在しないか、現在募集を締め切っています。", ephemeral=True)
            return

        guild = interaction.guild
        user = interaction.user
        channel_name = f"{dept_name}-{user.name}".lower()

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        base_admin_role = guild.get_role(ADMIN_ROLE_ID)
        if base_admin_role:
            for role in guild.roles:
                if role.position >= base_admin_role.position:
                    overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        category = interaction.channel.category
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"{user.display_name} さんの【{dept_name}】応募用チャンネルです。"
        )

        roles = [role.mention for role in user.roles if role.name != "@everyone"]
        roles_str = ", ".join(roles) if roles else "なし"
        joined_at_str = user.joined_at.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S JST") if user.joined_at else "不明"

        ready_embed = discord.Embed(
            title=f"📋 {dept_name} 応募手続き",
            description=f"{user.mention} さん、専用チャンネルを作成しました。\n\n**準備はできましたか？**\n以下のボタンを押すと質問を開始します。",
            color=discord.Color.green()
        )
        ready_embed.add_field(name="🆔 Discord ID", value=f"`{user.id}`", inline=False)
        ready_embed.add_field(name="📅 サーバー参加日", value=joined_at_str, inline=False)
        ready_embed.add_field(name="🎭 所持ロール一覧", value=roles_str, inline=False)

        view = ReadyCheckView(applicant=user, dept_name=dept_name, questions=dept_data.get("questions", []))
        await ticket_channel.send(content=user.mention, embed=ready_embed, view=view)

        await interaction.followup.send(f"✅ {dept_name}の専用チャンネルを作成しました: {ticket_channel.mention}", ephemeral=True)

class QuizUserPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(QuizUserPanelSelect())

class AddDeptModal(discord.ui.Modal, title="新しい部署の追加"):
    dept_name = discord.ui.TextInput(label="部署名を入力", placeholder="例: 広報部", required=True, max_length=20)

    def __init__(self, admin_msg: discord.Message, bot_ref: commands.Bot):
        super().__init__()
        self.admin_msg = admin_msg
        self.bot_ref = bot_ref

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        name = self.dept_name.value
        if name in quiz_config.get("departments", {}):
            await interaction.followup.send("⚠️ その部署は既に存在します。", ephemeral=True)
            return
         
        if "departments" not in quiz_config:
            quiz_config["departments"] = {}
         
        quiz_config["departments"][name] = {"is_open": True, "questions": []}
        await save_config_to_discord(self.bot_ref)
        try:
            await self.admin_msg.edit(embed=generate_admin_embed())
        except discord.NotFound:
            pass
        await interaction.followup.send(f"✅ 部署「{name}」を追加しました。", ephemeral=True)

class AddQuestionsModal(discord.ui.Modal):
    questions_text = discord.ui.TextInput(
        label="質問内容（1行につき1問）",
        style=discord.TextStyle.paragraph,
        placeholder="例:\n志望動機を教えてください\n得意な言語は何ですか",
        required=True,
        max_length=2000
    )

    def __init__(self, dept_name: str, admin_msg: discord.Message, bot_ref: commands.Bot):
        super().__init__(title=f"{dept_name}に質問を一括追加")
        self.dept_name = dept_name
        self.admin_msg = admin_msg
        self.bot_ref = bot_ref

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        dept_data = quiz_config.get("departments", {}).get(self.dept_name)
        if not dept_data:
            await interaction.followup.send("⚠️ 指定された部署が見つかりませんでした。", ephemeral=True)
            return

        lines = [line.strip() for line in self.questions_text.value.split("\n") if line.strip()]
        questions_list = dept_data.setdefault("questions", [])
        current_len = len(questions_list)
        added_count = 0
        for i, line in enumerate(lines):
            new_id = current_len + i + 1
            questions_list.append({"id": new_id, "question": line})
            added_count += 1

        await save_config_to_discord(self.bot_ref)
        try:
            await self.admin_msg.edit(embed=generate_admin_embed())
        except discord.NotFound:
            pass
             
        await interaction.followup.send(f"✅ 「{self.dept_name}」に **{added_count}問** の質問を追加しました！", ephemeral=True)

class SelectDeptView(discord.ui.View):
    def __init__(self, action: str, admin_msg: discord.Message, bot_ref: commands.Bot):
        super().__init__(timeout=60)
        self.action = action
        self.admin_msg = admin_msg
        self.bot_ref = bot_ref
         
        options = [discord.SelectOption(label=d, value=d) for d in quiz_config.get("departments", {}).keys()]
        if not options:
            options.append(discord.SelectOption(label="部署がありません", value="none"))
             
        select = discord.ui.Select(placeholder="対象の部署を選択してください...", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        dept = self.children[0].values[0]
        if dept == "none":
            await interaction.response.edit_message(content="部署が存在しません。", view=None)
            return
         
        if self.action == "delete":
            await interaction.response.defer()
            if dept in quiz_config.get("departments", {}):
                del quiz_config["departments"][dept]
            await save_config_to_discord(self.bot_ref)
            try:
                await self.admin_msg.edit(embed=generate_admin_embed())
            except discord.NotFound:
                pass
            await interaction.edit_original_response(content=f"🗑️ 部署「{dept}」を削除しました。", view=None)
             
        elif self.action == "toggle":
            await interaction.response.defer()
            if dept in quiz_config.get("departments", {}):
                quiz_config["departments"][dept]["is_open"] = not quiz_config["departments"][dept].get("is_open", True)
            await save_config_to_discord(self.bot_ref)
            try:
                await self.admin_msg.edit(embed=generate_admin_embed())
            except discord.NotFound:
                pass
            state = "🟢 募集開始" if quiz_config["departments"][dept]["is_open"] else "🔴 募集停止"
            await interaction.edit_original_response(content=f"🔄 「{dept}」を **{state}** に変更しました。", view=None)
             
        elif self.action == "add_question":
            await interaction.response.send_modal(AddQuestionsModal(dept, self.admin_msg, self.bot_ref))
            try:
                await interaction.message.delete()
            except discord.NotFound:
                pass

class AdminPanelEditView(discord.ui.View):
    def __init__(self, bot_ref: commands.Bot = None):
        super().__init__(timeout=None)
        self.bot_ref = bot_ref

    @discord.ui.button(label="➕ 部署追加", style=discord.ButtonStyle.primary, custom_id="admin_add_dept_btn")
    async def add_dept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_role_or_higher(interaction.user):
            await interaction.response.send_message("⚠️ この操作を実行する権限がありません。", ephemeral=True)
            return
        await interaction.response.send_modal(AddDeptModal(interaction.message, interaction.client))

    @discord.ui.button(label="🗑️ 部署削除", style=discord.ButtonStyle.danger, custom_id="admin_del_dept_btn")
    async def del_dept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_role_or_higher(interaction.user):
            await interaction.response.send_message("⚠️ この操作を実行する権限がありません。", ephemeral=True)
            return
        if not quiz_config.get("departments"):
            await interaction.response.send_message("⚠️ 削除できる部署がありません。", ephemeral=True)
            return
        await interaction.response.send_message("削除する部署を選んでください:", view=SelectDeptView("delete", interaction.message, interaction.client), ephemeral=True)

    @discord.ui.button(label="❓ 質問追加", style=discord.ButtonStyle.secondary, custom_id="admin_add_question_btn")
    async def add_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_role_or_higher(interaction.user):
            await interaction.response.send_message("⚠️ この操作を実行する権限がありません。", ephemeral=True)
            return
        if not quiz_config.get("departments"):
            await interaction.response.send_message("⚠️ 先に「部署追加」を行ってください。", ephemeral=True)
            return
        await interaction.response.send_message("質問を追加する部署を選んでください:", view=SelectDeptView("add_question", interaction.message, interaction.client), ephemeral=True)

    @discord.ui.button(label="🔄 募集ON/OFF機能", style=discord.ButtonStyle.secondary, custom_id="admin_toggle_status_btn")
    async def toggle_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_role_or_higher(interaction.user):
            await interaction.response.send_message("⚠️ この操作を実行する権限がありません。", ephemeral=True)
            return
        if not quiz_config.get("departments"):
            await interaction.response.send_message("⚠️ 設定する部署がありません。", ephemeral=True)
            return
        await interaction.response.send_message("ON/OFFを切り替える部署を選んでください:", view=SelectDeptView("toggle", interaction.message, interaction.client), ephemeral=True)

    @discord.ui.button(label="📌 パネル送信", style=discord.ButtonStyle.success, custom_id="admin_send_panel_btn")
    async def send_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_role_or_higher(interaction.user):
            await interaction.response.send_message("⚠️ この操作を実行する権限がありません。", ephemeral=True)
            return
        # 3秒以内応答ルール対策：重い処理(channel.send等)の前に必ず先にdeferしてACKする
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="✨ 自己推薦・応募受付",
            description="下のメニューから応募したい部署を選択してください。",
            color=discord.Color.green()
        )
        await interaction.channel.send(embed=embed, view=QuizUserPanelView())
        await interaction.followup.send("✅ このチャンネルに応募用パネルを送信しました！", ephemeral=True)

async def auto_send_user_panel(bot: commands.Bot):
    """起動時に設定データをロードし、永続Viewを登録して指定のチャンネルへ応募パネルを自動更新・送信する"""
    await bot.wait_until_ready()
     
    await load_config_from_discord(bot)
     
    bot.add_view(AdminPanelEditView(bot))
    bot.add_view(QuizUserPanelView())
    bot.add_view(ReadyCheckView())

    try:
        channel = bot.get_channel(PANEL_AUTO_SEND_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(PANEL_AUTO_SEND_CHANNEL_ID)
         
        if channel:
            embed = discord.Embed(
                title="✨ 自己推薦・応募受付",
                description="下のメニューから応募したい部署を選択してください。",
                color=discord.Color.green()
            )
             
            existing_message = None
            async for msg in channel.history(limit=20):
                if msg.author.id == bot.user.id and msg.embeds:
                    if msg.embeds[0].title == "✨ 自己推薦・応募受付":
                        existing_message = msg
                        break

            if existing_message:
                await existing_message.edit(embed=embed, view=QuizUserPanelView())
            else:
                await channel.send(embed=embed, view=QuizUserPanelView())

    except Exception as e:
        print(f"❌ [Quiz] 応募パネルの送信・更新に失敗しました: {e}")

# ==========================================
# 【追加】各種申請BOT機能 - 機能1：パネル生成
# ==========================================
class ApplicationPanelSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=t, value=t) for t in APPLICATION_TYPES]
        super().__init__(
            placeholder="申請の種類を選択",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="hr_panel_select_persistent",
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "休職申請":
            await interaction.response.send_modal(LeaveApplicationModal())
        elif choice == "転属申請":
            await interaction.response.send_modal(TransferApplicationModal())
        elif choice == "兼務申請":
            await interaction.response.send_modal(ConcurrentApplicationModal())
        elif choice == "退職申請":
            await interaction.response.send_modal(ResignationApplicationModal())


class ApplicationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ApplicationPanelSelect())


def generate_application_panel_embed() -> discord.Embed:
    return discord.Embed(
        title="各種申請",
        description="転属・兼務・休職・退職申請専用です。",
        color=discord.Color.blurple(),
    )


# ==========================================
# 【追加】各種申請BOT機能 - 機能2：申請フォーム（Modal）とチケット作成
# ==========================================
async def create_application_ticket(interaction: discord.Interaction, app_type: str, answers: dict):
    """Modal送信後に共通で呼び出す、チケットチャンネル作成処理"""
    guild = interaction.guild
    user = interaction.user

    category = guild.get_channel(TICKET_CATEGORY_ID)
    if category is None:
        try:
            category = await guild.fetch_channel(TICKET_CATEGORY_ID)
        except Exception as e:
            await interaction.followup.send(f"❌ チケット作成先カテゴリーの取得に失敗しました: {e}", ephemeral=True)
            return

    channel_name = sanitize_channel_name(f"{app_type}-{user.name}")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
    }
    admin_role = guild.get_role(ADMIN_ROLE_ID)
    if admin_role:
        for role in guild.roles:
            if role.position >= admin_role.position and role != guild.default_role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    try:
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"{user.display_name} さんの【{app_type}】申請チャンネルです。",
        )
    except Exception as e:
        await interaction.followup.send(f"❌ チケットチャンネルの作成に失敗しました: {e}", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"📋 {app_type} - {user.display_name}",
        description=f"{user.mention} さんより申請が提出されました。内容を確認し、下のボタンで判定してください。",
        color=discord.Color.orange(),
    )
    for label, value in answers.items():
        embed.add_field(name=label, value=value if value else "（未入力）", inline=False)
    embed.set_footer(text=f"UserID:{user.id}|Type:{app_type}")

    await ticket_channel.send(content=user.mention, embed=embed, view=ApprovalView())
    await interaction.followup.send(f"✅ {app_type}を受け付けました: {ticket_channel.mention}", ephemeral=True)


class LeaveApplicationModal(discord.ui.Modal, title="休職申請"):
    period = discord.ui.TextInput(label="休職期間", placeholder="YYYY/MM/DD〜YYYY/MM/DD", required=True, max_length=50)
    reason = discord.ui.TextInput(label="休職理由", style=discord.TextStyle.paragraph, required=True, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        start, end = parse_period(self.period.value)
        if not end:
            await interaction.response.send_message(
                "⚠️ 休職期間の形式が正しくありません。『YYYY/MM/DD〜YYYY/MM/DD』の形式で入力してください。",
                ephemeral=True,
            )
            return
        try:
            datetime.datetime.strptime(end.strip(), "%Y/%m/%d")
        except ValueError:
            await interaction.response.send_message(
                "⚠️ 復職予定日（期間の後半）の日付形式が正しくありません。『YYYY/MM/DD』で入力してください。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await create_application_ticket(
            interaction, "休職申請", {"休職期間": self.period.value, "休職理由": self.reason.value}
        )


class TransferApplicationModal(discord.ui.Modal, title="転属申請"):
    current_dept = discord.ui.TextInput(label="現在所属部署", required=True, max_length=50)
    new_dept = discord.ui.TextInput(label="転属希望部署", required=True, max_length=50)
    reason = discord.ui.TextInput(label="転属理由", style=discord.TextStyle.paragraph, required=True, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_application_ticket(
            interaction,
            "転属申請",
            {
                "現在所属部署": self.current_dept.value,
                "転属希望部署": self.new_dept.value,
                "転属理由": self.reason.value,
            },
        )


class ConcurrentApplicationModal(discord.ui.Modal, title="兼務申請"):
    current_dept = discord.ui.TextInput(label="現在所属部署", required=True, max_length=50)
    new_post = discord.ui.TextInput(label="兼務希望先", required=True, max_length=50)
    reason = discord.ui.TextInput(label="兼務希望理由", style=discord.TextStyle.paragraph, required=True, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_application_ticket(
            interaction,
            "兼務申請",
            {
                "現在所属部署": self.current_dept.value,
                "兼務希望先": self.new_post.value,
                "兼務希望理由": self.reason.value,
            },
        )


class ResignationApplicationModal(discord.ui.Modal, title="退職申請"):
    current_dept = discord.ui.TextInput(label="現在所属部署", required=True, max_length=50)
    resign_date = discord.ui.TextInput(label="退職日", placeholder="YYYY/MM/DD", required=True, max_length=20)
    reason = discord.ui.TextInput(label="退職理由", style=discord.TextStyle.paragraph, required=True, max_length=1000)
    notes = discord.ui.TextInput(label="備考", style=discord.TextStyle.paragraph, required=False, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_application_ticket(
            interaction,
            "退職申請",
            {
                "現在所属部署": self.current_dept.value,
                "退職日": self.resign_date.value,
                "退職理由": self.reason.value,
                "備考": self.notes.value or "（なし）",
            },
        )


# ==========================================
# 【追加】各種申請BOT機能 - 機能3：チケット内の承認・不承認処理
# ==========================================
async def close_hr_ticket_channel(channel: discord.TextChannel, delay: int = 5):
    await asyncio.sleep(delay)
    try:
        await channel.delete(reason="申請処理が完了したため")
    except Exception as e:
        print(f"❌ [HR] チケットチャンネルの削除に失敗しました: {e}")


async def send_hr_log(bot: commands.Bot, embed: discord.Embed):
    log_channel = await get_hr_log_channel(bot)
    if log_channel:
        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"❌ [HR] ログ送信に失敗しました: {e}")


class _AbortApproval(Exception):
    """許可処理を中断するための内部例外（チャンネルはクローズしない）"""
    pass


class DenyReasonModal(discord.ui.Modal, title="不許可理由入力"):
    deny_reason = discord.ui.TextInput(
        label="不許可の理由", style=discord.TextStyle.paragraph, required=True, max_length=1000
    )

    def __init__(self, ticket_message: discord.Message):
        super().__init__()
        self.ticket_message = ticket_message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        embed = self.ticket_message.embeds[0] if self.ticket_message.embeds else None
        if embed is None:
            await interaction.followup.send("❌ チケット情報の取得に失敗しました。", ephemeral=True)
            return

        user_id_str, app_type = parse_ticket_footer(embed)
        guild = interaction.guild
        member = None
        if user_id_str:
            member = guild.get_member(int(user_id_str))
            if member is None:
                try:
                    member = await guild.fetch_member(int(user_id_str))
                except Exception:
                    member = None

        reason_text = self.deny_reason.value

        notify_embed = discord.Embed(
            title="❌ 申請が不許可となりました",
            description=f"**申請種別:** {app_type}\n**理由:**\n{reason_text}",
            color=discord.Color.red(),
        )
        dm_sent = False
        if member:
            try:
                await member.send(embed=notify_embed)
                dm_sent = True
            except Exception:
                dm_sent = False

        try:
            await self.ticket_message.channel.send(content=member.mention if member else None, embed=notify_embed)
        except Exception:
            pass

        log_embed = discord.Embed(
            title="🚫 申請 不許可ログ",
            description=(
                f"**申請種別:** {app_type}\n"
                f"**対象者:** {member.mention if member else f'ID:{user_id_str}'}\n"
                f"**処理者:** {interaction.user.mention}\n"
                f"**理由:** {reason_text}\n"
                f"**DM通知:** {'成功' if dm_sent else '失敗（チケット内通知のみ）'}"
            ),
            color=discord.Color.red(),
            timestamp=datetime.datetime.now(JST),
        )
        await send_hr_log(interaction.client, log_embed)

        await interaction.followup.send("✅ 不許可処理を完了しました。", ephemeral=True)


class ApprovalView(discord.ui.View):
    """チケット内の許可／不可ボタン。状態はEmbedから復元するため再起動にも耐える"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="許可", style=discord.ButtonStyle.success, custom_id="hr_approve_btn")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_role_or_higher(interaction.user):
            await interaction.response.send_message("⚠️ この操作を実行する権限がありません。", ephemeral=True)
            return
        await interaction.response.defer()

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed is None:
            await interaction.followup.send("❌ チケット情報の取得に失敗しました。", ephemeral=True)
            return

        user_id_str, app_type = parse_ticket_footer(embed)
        guild = interaction.guild
        if not user_id_str:
            await interaction.followup.send("❌ 申請者情報の取得に失敗しました。", ephemeral=True)
            return

        member = guild.get_member(int(user_id_str))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id_str))
            except Exception:
                await interaction.followup.send("❌ 申請者がサーバーに見つかりませんでした。", ephemeral=True)
                return

        detail_lines = []
        try:
            if app_type == "休職申請":
                await self._handle_leave(interaction, guild, member, embed, detail_lines)
            elif app_type == "転属申請":
                await self._handle_transfer(interaction, guild, member, embed, detail_lines)
            elif app_type == "兼務申請":
                await self._handle_concurrent(interaction, guild, member, embed, detail_lines)
            elif app_type == "退職申請":
                await self._handle_resignation(interaction, guild, member, embed, detail_lines)
            else:
                await interaction.followup.send("❌ 未知の申請種別です。", ephemeral=True)
                return
        except _AbortApproval:
            return
        except Exception as e:
            await interaction.followup.send(f"❌ 処理中にエラーが発生しました: {e}", ephemeral=True)
            return

        log_embed = discord.Embed(
            title="✅ 申請 許可ログ",
            description=(
                f"**申請種別:** {app_type}\n"
                f"**対象者:** {member.mention}\n"
                f"**処理者:** {interaction.user.mention}\n" + "\n".join(detail_lines)
            ),
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(JST),
        )
        await send_hr_log(interaction.client, log_embed)

        try:
            await interaction.channel.send(f"{member.mention} 申請が許可されました。")
        except Exception:
            pass

        await interaction.followup.send("✅ 許可処理を完了しました。", ephemeral=True)

    @discord.ui.button(label="不可", style=discord.ButtonStyle.danger, custom_id="hr_deny_btn")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_role_or_higher(interaction.user):
            await interaction.response.send_message("⚠️ この操作を実行する権限がありません。", ephemeral=True)
            return
        await interaction.response.send_modal(DenyReasonModal(interaction.message))

    async def _handle_leave(self, interaction, guild, member, embed, detail_lines):
        period_str = get_field_value(embed, "休職期間")
        start, end = parse_period(period_str)
        if not end:
            await interaction.followup.send("❌ 休職期間の解析に失敗しました。手動で対応してください。", ephemeral=True)
            raise _AbortApproval()

        leave_role = find_role_by_name(guild, LEAVE_ROLE_NAME)
        if not leave_role:
            await interaction.followup.send(
                f"❌ ロール「{LEAVE_ROLE_NAME}」がサーバーに存在しません。ロールを作成してから再度お試しください。",
                ephemeral=True,
            )
            raise _AbortApproval()

        await member.add_roles(leave_role, reason="休職申請承認")

        db = get_firestore_client()
        if db:
            try:
                db.collection(FIRESTORE_COLLECTION).document(str(member.id)).set(
                    {"user_id": member.id, "guild_id": guild.id, "end_date": end}
                )
            except Exception as e:
                print(f"❌ [HR] Firestore保存エラー: {e}")
        else:
            print("⚠️ [HR] Firebase未接続のため復職リマインドは登録されませんでした。")

        detail_lines.append(f"**付与ロール:** {leave_role.mention}\n**復職予定日:** {end}")

    async def _handle_transfer(self, interaction, guild, member, embed, detail_lines):
        current_name = get_field_value(embed, "現在所属部署")
        new_name = get_field_value(embed, "転属希望部署")

        current_role = find_role_by_name(guild, current_name)
        new_role = find_role_by_name(guild, new_name)

        missing = []
        if not current_role:
            missing.append(current_name)
        if not new_role:
            missing.append(new_name)
        if missing:
            await interaction.followup.send(
                f"❌ 以下の部署に対応するロールが見つかりませんでした: {', '.join(missing)}\nロール名を確認のうえ、手動で対応してください。",
                ephemeral=True,
            )
            raise _AbortApproval()

        if current_role in member.roles:
            await member.remove_roles(current_role, reason="転属申請承認")
        await member.add_roles(new_role, reason="転属申請承認")

        detail_lines.append(f"**剥奪ロール:** {current_role.mention}\n**付与ロール:** {new_role.mention}")

    async def _handle_concurrent(self, interaction, guild, member, embed, detail_lines):
        new_name = get_field_value(embed, "兼務希望先")
        new_role = find_role_by_name(guild, new_name)
        if not new_role:
            await interaction.followup.send(
                f"❌ ロール「{new_name}」が見つかりませんでした。ロール名を確認のうえ、手動で対応してください。",
                ephemeral=True,
            )
            raise _AbortApproval()

        await member.add_roles(new_role, reason="兼務申請承認")
        detail_lines.append(f"**追加付与ロール:** {new_role.mention}（既存ロールは維持）")

    async def _handle_resignation(self, interaction, guild, member, embed, detail_lines):
        base_role = find_role_by_name(guild, BASE_MEMBER_ROLE_NAME)
        if not base_role:
            await interaction.followup.send(
                f"❌ 基準ロール「{BASE_MEMBER_ROLE_NAME}」が見つかりませんでした。ロール名を確認のうえ、手動で対応してください。",
                ephemeral=True,
            )
            raise _AbortApproval()

        roles_to_remove = [r for r in member.roles if r != guild.default_role and r.position > base_role.position]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="退職申請承認")

        role_names = ", ".join(r.name for r in roles_to_remove) if roles_to_remove else "（該当なし）"
        detail_lines.append(f"**剥奪ロール:** {role_names}")


# ==========================================
# 【追加】各種申請BOT機能 - 機能4：Firebase 自動復職リマインド
# ==========================================
class ReinstateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="復職する", style=discord.ButtonStyle.success, custom_id="hr_reinstate_btn")
    async def reinstate(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin_role_or_higher(interaction.user):
            await interaction.response.send_message("⚠️ この操作を実行する権限がありません。", ephemeral=True)
            return
        await interaction.response.defer()

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        user_id_str, _ = parse_ticket_footer(embed) if embed else (None, None)
        if not user_id_str:
            await interaction.followup.send("❌ 対象者情報の取得に失敗しました。", ephemeral=True)
            return

        guild = interaction.guild
        member = guild.get_member(int(user_id_str))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id_str))
            except Exception:
                member = None

        leave_role = find_role_by_name(guild, LEAVE_ROLE_NAME)
        if member and leave_role and leave_role in member.roles:
            await member.remove_roles(leave_role, reason="復職処理")

        db = get_firestore_client()
        if db:
            try:
                db.collection(FIRESTORE_COLLECTION).document(str(user_id_str)).delete()
            except Exception as e:
                print(f"❌ [HR] Firestore削除エラー: {e}")

        button.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        log_embed = discord.Embed(
            title="🔓 復職処理ログ",
            description=f"**対象者:** {member.mention if member else f'ID:{user_id_str}'}\n**処理者:** {interaction.user.mention}",
            color=discord.Color.blue(),
            timestamp=datetime.datetime.now(JST),
        )
        await send_hr_log(interaction.client, log_embed)
        await interaction.followup.send("✅ 復職処理を完了しました。", ephemeral=True)


class ReinstateChecker:
    """毎日0:00(JST)に休職者の復職予定日をチェックし、該当者がいればリマインドを送信する。
    さらに、Bot再起動のたびにも即座に一度チェックを行い、停止中に見逃していた
    （予定日を過ぎてしまった）復職者についても改めてリマインドできるようにする。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_loop.start()

    def stop(self):
        self.check_loop.cancel()

    async def run_check(self, include_overdue: bool = False):
        """休職者情報(Firestore)を確認し、該当者がいればリマインドを送信する共通処理。
        include_overdue=True の場合、復職予定日を過ぎてしまった（Bot停止中に見逃した）
        休職者もあわせて対象にする（再起動時のチェック用）。"""
        db = get_firestore_client()
        if db is None:
            return

        now_jst = datetime.datetime.now(JST)
        today_str = now_jst.strftime("%Y/%m/%d")
        log_channel = await get_hr_log_channel(self.bot)
        if log_channel is None:
            return

        try:
            docs = db.collection(FIRESTORE_COLLECTION).stream()
        except Exception as e:
            print(f"❌ [HR] Firestore読み込みエラー: {e}")
            return

        for doc in docs:
            data = doc.to_dict() or {}
            end_date_str = data.get("end_date")
            if not end_date_str:
                continue

            is_due = end_date_str == today_str
            is_overdue = False
            if not is_due and include_overdue:
                try:
                    end_dt = datetime.datetime.strptime(end_date_str.strip(), "%Y/%m/%d")
                    if end_dt.date() < now_jst.date():
                        is_due = True
                        is_overdue = True
                except ValueError:
                    pass

            if not is_due:
                continue

            user_id = data.get("user_id")
            guild_id = data.get("guild_id")
            guild = self.bot.get_guild(guild_id) if guild_id else (log_channel.guild if log_channel else None)
            member = guild.get_member(user_id) if guild else None
            mention = member.mention if member else f"ID:{user_id}"

            overdue_note = "\n⚠️ 復職予定日を過ぎています（Bot停止中の見逃しの可能性があります）。" if is_overdue else ""
            embed = discord.Embed(
                title="🔔 復職リマインド",
                description=f"{mention} さんの休職期間が終了しました。復職しますか？\n**復職予定日:** {end_date_str}{overdue_note}",
                color=discord.Color.gold(),
            )
            embed.set_footer(text=f"UserID:{user_id}|Type:復職リマインド")

            try:
                await log_channel.send(content=mention, embed=embed, view=ReinstateView())
            except Exception as e:
                print(f"❌ [HR] 復職リマインド送信エラー: {e}")

    @tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=JST))
    async def check_loop(self):
        await self.run_check(include_overdue=False)

    @check_loop.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()
        # Bot再起動のたびに一度、復職予定日を確認する（見逃し分もチェック）
        try:
            await self.run_check(include_overdue=True)
        except Exception as e:
            print(f"❌ [HR] 再起動時の復職チェックに失敗しました: {e}")


# ==========================================
# 【追加】Bot再起動のたびに、作成済みの申請・応募チャンネルを確認する処理
# ==========================================
async def audit_created_channels_on_startup(bot: commands.Bot):
    """Bot再起動のたびに実行し、これまでに作成された申請/応募用チャンネルで
    処理されずに残っているものがないかを確認し、ログチャンネルへ報告する。"""
    await bot.wait_until_ready()
    log_channel = await get_hr_log_channel(bot)
    now_jst = datetime.datetime.now(JST)

    for guild in bot.guilds:
        remaining = []
        for channel in guild.text_channels:
            topic = channel.topic or ""
            # create_application_ticket / QuizUserPanelSelect が作成するチャンネルのtopicで判定
            if "申請チャンネルです。" in topic or "応募用チャンネルです。" in topic:
                created_jst = channel.created_at.astimezone(JST)
                remaining.append((channel, created_jst))

        if not remaining:
            continue

        remaining.sort(key=lambda x: x[1])
        lines = []
        for ch, created in remaining:
            elapsed_hours = int((now_jst - created).total_seconds() // 3600)
            lines.append(f"・{ch.mention}（作成: {created.strftime('%Y/%m/%d %H:%M')} JST／経過約{elapsed_hours}時間）")

        embed = discord.Embed(
            title="🔁 再起動チェック：未処理チャンネルの確認",
            description=(
                f"Botの再起動を検知したため、未処理のまま残っている申請・応募チャンネルを確認しました。\n"
                f"（サーバー: {guild.name}／該当 {len(remaining)}件）\n\n" + "\n".join(lines)
            ),
            color=discord.Color.orange(),
            timestamp=now_jst,
        )
        if log_channel:
            try:
                await log_channel.send(embed=embed)
            except Exception as e:
                print(f"❌ [Startup] 未処理チャンネル報告の送信に失敗しました: {e}")
        else:
            print(f"⚠️ [Startup] ログチャンネル未取得のため、{guild.name} の未処理チャンネル報告をスキップしました。")


# ==========================================
# 【追加】運転整理チケットBOT機能（新チケットシステム）
# ==========================================
NEW_TICKET_CATEGORY_ID = 1510021468220690457              # 新チケット作成先カテゴリーID
NEW_TICKET_VIEWER_ROLE_IDS = [1510021467167789097, 1510405214811852900]  # チケット閲覧可能ロールID
TICKET_STAFF_OVERRIDE_USER_ID = 1301944996261400656        # 担当制限を無視できるユーザーID
TICKET_STAFF_OVERRIDE_ROLE_ID = 1543754312453464185        # 担当制限を無視できるロールID
TICKET_MANAGEMENT_CHANNEL_ID = 1544534764642639873         # 担当記録用管理チャンネルID
NEW_TICKET_FIRESTORE_COLLECTION = "new_ticket_status"       # 新チケット情報を保存するFirestoreコレクション名

# チケット開設時に案内するフォーマットの項目（ラベル → 内部キー）
NEW_TICKET_FIELD_LABELS = {
    "作成難易度": "difficulty",
    "運転区間": "section",
    "種別": "type",
    "運転時間帯": "drive_time",
    "運転士の数": "driver_count",
    "退避設定": "evacuation",
    "接続設定": "connection",
    "期限": "deadline",
    "その他要望": "other_requests",
}

_new_ticket_number_lock = asyncio.Lock()


def has_ticket_override(member: discord.Member) -> bool:
    """担当者の制限を無視してすべてのコマンド・チケット操作を実行できるか判定"""
    if not isinstance(member, discord.Member):
        return False
    if member.id == TICKET_STAFF_OVERRIDE_USER_ID:
        return True
    if any(r.id == TICKET_STAFF_OVERRIDE_ROLE_ID for r in member.roles):
        return True
    return False


def can_view_new_tickets(member: discord.Member) -> bool:
    """新チケットの閲覧・操作権限（指定ロール、または特権ユーザー/ロール）を持つか判定"""
    if not isinstance(member, discord.Member):
        return False
    if has_ticket_override(member):
        return True
    return any(r.id in NEW_TICKET_VIEWER_ROLE_IDS for r in member.roles)


def parse_ticket_topic(topic: str):
    """チャンネルtopicに保存した新チケットのメタデータを辞書として復元する"""
    if not topic or not topic.startswith("NewTicketV1"):
        return None
    data = {}
    for part in topic.split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            data[k.strip()] = v.strip()
    return data


def _build_ticket_topic_string(data: dict) -> str:
    fields = ["number", "requester", "difficulty", "section", "type", "drive_time",
              "driver_count", "evacuation", "connection", "deadline", "other_requests",
              "requested", "assignee", "finished", "log_msg", "guild"]
    return "|".join(["NewTicketV1"] + [f"{f}:{data.get(f, '')}" for f in fields])


def save_ticket_to_firestore(data: dict):
    """新チケットのメタデータをFirestoreへ保存する（channel_idをドキュメントIDとする）。DB未設定時は何もしない"""
    channel_id = data.get("channel_id") or data.get("channel")
    if not channel_id:
        return
    db_client = get_firestore_client()
    if db_client is None:
        return
    try:
        db_client.collection(NEW_TICKET_FIRESTORE_COLLECTION).document(str(channel_id)).set(data, merge=True)
    except Exception as e:
        print(f"❌ [Ticket] Firestoreへの保存に失敗しました: {e}")


def load_ticket_from_firestore(channel_id):
    """Firestoreから指定チャンネルのチケットメタデータを取得する。DB未設定・未登録時はNone"""
    db_client = get_firestore_client()
    if db_client is None:
        return None
    try:
        doc = db_client.collection(NEW_TICKET_FIRESTORE_COLLECTION).document(str(channel_id)).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"❌ [Ticket] Firestoreからの取得に失敗しました: {e}")
        return None


async def update_ticket_channel_topic(channel: discord.TextChannel, **updates) -> dict:
    """新チケットのメタデータを一部更新し、チャンネルtopicとFirestoreの両方へ保存する"""
    data = parse_ticket_topic(channel.topic) or {}
    for k, v in updates.items():
        data[k] = "" if v is None else str(v)
    data["channel_id"] = str(channel.id)
    data["guild"] = str(channel.guild.id) if channel.guild else data.get("guild", "")

    new_topic = _build_ticket_topic_string(data)
    try:
        await channel.edit(topic=new_topic)
    except Exception as e:
        print(f"❌ [Ticket] トピック更新に失敗しました: {e}")

    save_ticket_to_firestore(data)
    return data


def is_new_ticket_channel(channel) -> bool:
    return isinstance(channel, discord.TextChannel) and parse_ticket_topic(channel.topic) is not None


def parse_ticket_format_message(content: str) -> dict:
    """依頼者が送信したフォーマットメッセージから『ラベル：値』の形式で入力された項目を抽出する（自動読み取り）"""
    updates = {}
    if not content:
        return updates
    for line in content.splitlines():
        line = line.strip()
        for label, key in NEW_TICKET_FIELD_LABELS.items():
            m = re.match(rf"^{re.escape(label)}\s*[:：]\s*(.+)$", line)
            if m:
                value = m.group(1).strip()
                if value:
                    updates[key] = value
                break
    return updates


async def get_new_ticket_category(guild: discord.Guild):
    category = guild.get_channel(NEW_TICKET_CATEGORY_ID)
    if category is None:
        try:
            category = await guild.fetch_channel(NEW_TICKET_CATEGORY_ID)
        except Exception as e:
            print(f"❌ [Ticket] チケット作成先カテゴリーの取得に失敗しました: {e}")
            return None
    return category


async def get_ticket_management_channel(bot: commands.Bot):
    channel = bot.get_channel(TICKET_MANAGEMENT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(TICKET_MANAGEMENT_CHANNEL_ID)
        except Exception as e:
            print(f"❌ [Ticket] 管理チャンネルの取得に失敗しました: {e}")
            return None
    return channel


async def next_ticket_number(category: discord.CategoryChannel) -> str:
    """カテゴリー内の既存チケット番号の最大値+1を4桁ゼロ埋めで返す（Bot再起動後も安全）"""
    max_num = 0
    for ch in (category.channels if category else []):
        m = re.match(r"(?:finished-)?ticket-(\d{4})", ch.name)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"{max_num + 1:04d}"


# チケット開設時に依頼者へ送信する記入フォーマット（{mention} は依頼者へのメンションに置換）
NEW_TICKET_INTAKE_MESSAGE_TEMPLATE = (
    "{mention} お問い合わせありがとうございます。スムーズな対応のため、"
    "担当者が対応する前に以下の内容をご記入ください。\n"
    "作成難易度：\n"
    "（初心者 / 中級者 / 上級者）\n"
    "運転区間：\n"
    "種別：\n"
    "運転時間帯：\n"
    "運転士の数：\n"
    "退避設定：\n"
    "接続設定：\n"
    "期限：\n"
    "（もしあれば）\n"
    "その他要望："
)


async def create_new_ticket_channel(interaction: discord.Interaction):
    guild = interaction.guild
    user = interaction.user

    category = await get_new_ticket_category(guild)
    if category is None:
        await interaction.followup.send("❌ チケット作成先カテゴリーが見つかりませんでした。", ephemeral=True)
        return

    async with _new_ticket_number_lock:
        number = await next_ticket_number(category)
        channel_name = f"ticket-{number}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }
        for role_id in NEW_TICKET_VIEWER_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        override_role = guild.get_role(TICKET_STAFF_OVERRIDE_ROLE_ID)
        if override_role:
            overwrites[override_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        requested_date = datetime.datetime.now(JST).strftime("%Y/%m/%d")

        try:
            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=_build_ticket_topic_string({
                    "number": number,
                    "requester": user.id,
                    "requested": requested_date,
                }),
            )
        except Exception as e:
            await interaction.followup.send(f"❌ チケットチャンネルの作成に失敗しました: {e}", ephemeral=True)
            return

    await update_ticket_channel_topic(
        ticket_channel, number=number, requester=user.id, requested=requested_date,
    )

    await ticket_channel.send(NEW_TICKET_INTAKE_MESSAGE_TEMPLATE.format(mention=user.mention))
    await interaction.followup.send(f"✅ チケットを作成しました: {ticket_channel.mention}", ephemeral=True)


class NewTicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="依頼する", emoji="📩", style=discord.ButtonStyle.secondary, custom_id="new_ticket_panel_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await create_new_ticket_channel(interaction)


def generate_new_ticket_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="作成依頼",
        description="ダイヤ作成依頼専用です。チケットを開くには下の「依頼する」ボタンを押してください。",
        color=discord.Color.green(),
    )
    return embed


async def send_or_update_ticket_log(bot: commands.Bot, channel: discord.TextChannel, data: dict):
    """管理チャンネルへチケット情報Embedを新規送信、既にあれば更新する"""
    log_channel = await get_ticket_management_channel(bot)
    if log_channel is None:
        return None

    guild = channel.guild
    requester = guild.get_member(int(data["requester"])) if data.get("requester") else None
    assignee = guild.get_member(int(data["assignee"])) if data.get("assignee") else None

    embed = discord.Embed(title=f"🎫 チケット #{data.get('number', '----')}", color=discord.Color.blurple())
    embed.add_field(name="チケット名", value=channel.mention, inline=False)
    embed.add_field(name="運転区間", value=data.get("section") or "未設定", inline=True)
    embed.add_field(name="担当者", value=assignee.mention if assignee else "未定", inline=True)
    embed.add_field(name="依頼者", value=requester.mention if requester else "不明", inline=True)
    embed.add_field(name="依頼日", value=data.get("requested") or "不明", inline=True)
    embed.add_field(name="対応終了日", value=data.get("finished") or "対応中", inline=True)

    log_msg_id = data.get("log_msg")
    if log_msg_id:
        try:
            msg = await log_channel.fetch_message(int(log_msg_id))
            await msg.edit(embed=embed)
            return msg.id
        except Exception:
            pass  # メッセージが見つからない場合は新規送信にフォールバック

    try:
        msg = await log_channel.send(embed=embed)
        return msg.id
    except Exception as e:
        print(f"❌ [Ticket] 管理ログの送信に失敗しました: {e}")
        return None


async def refresh_ticket_channel_name(channel: discord.TextChannel, data: dict, finished: bool = False):
    number = data.get("number", "----")
    if finished:
        new_name = f"finished-ticket-{number}"
    else:
        assignee_id = data.get("assignee")
        if assignee_id:
            member = channel.guild.get_member(int(assignee_id))
            new_name = f"ticket-{number} ({member.name if member else assignee_id})"
        else:
            new_name = f"ticket-{number}"
    try:
        await channel.edit(name=new_name[:100])
    except Exception as e:
        print(f"❌ [Ticket] チャンネル名変更に失敗しました: {e}")


async def audit_new_tickets_on_startup(bot: commands.Bot):
    """Bot再起動を検知するたびに実行し、Firestoreに保存された新チケット情報を読み込んで
    現存するチケットチャンネルのtopicを復元する（コマンドが再起動後も正しく動くようにするため）。"""
    await bot.wait_until_ready()
    db_client = get_firestore_client()
    if db_client is None:
        print("⚠️ [Ticket] Firestore未設定のため、新チケットの再起動時復元をスキップしました。")
        return

    try:
        docs = list(db_client.collection(NEW_TICKET_FIRESTORE_COLLECTION).stream())
    except Exception as e:
        print(f"❌ [Ticket] Firestoreからのチケット一覧取得に失敗しました: {e}")
        return

    restored, missing = 0, []
    for doc in docs:
        data = doc.to_dict() or {}
        if data.get("finished"):
            continue  # 対応終了済みのチケットは復元不要

        channel_id = data.get("channel_id") or doc.id
        guild_id = data.get("guild")
        guild = bot.get_guild(int(guild_id)) if guild_id else None

        channel = bot.get_channel(int(channel_id)) if channel_id else None
        if channel is None and guild:
            try:
                channel = await guild.fetch_channel(int(channel_id))
            except Exception:
                channel = None

        if channel is None:
            missing.append(data.get("number", channel_id))
            continue

        expected_topic = _build_ticket_topic_string(data)
        if channel.topic != expected_topic:
            try:
                await channel.edit(topic=expected_topic)
            except Exception as e:
                print(f"❌ [Ticket] {channel.name} のtopic復元に失敗しました: {e}")
        restored += 1

    print(f"✅ [Ticket] 再起動時のチケット情報復元が完了しました（復元:{restored}件／消失:{len(missing)}件）。")

    if missing:
        log_channel = await get_ticket_management_channel(bot)
        if log_channel:
            embed = discord.Embed(
                title="🔁 再起動チェック：チケットチャンネルの確認",
                description="Firestoreに記録があるものの、現存が確認できなかったチケットがあります：\n"
                            + "\n".join(f"・#{n}" for n in missing),
                color=discord.Color.orange(),
                timestamp=datetime.datetime.now(JST),
            )
            try:
                await log_channel.send(embed=embed)
            except Exception as e:
                print(f"❌ [Ticket] 再起動チェック結果の送信に失敗しました: {e}")


def setup_quiz_commands(bot: commands.Bot, *args, **kwargs):
    @bot.listen('on_ready')
    async def on_quiz_ready():
        if not hasattr(bot, '_quiz_auto_send_task') or bot._quiz_auto_send_task.done():
            bot._quiz_auto_send_task = bot.loop.create_task(auto_send_user_panel(bot))

    # ---- 【追加】各種申請BOT機能の初期化 ----
    @bot.listen('on_ready')
    async def on_hr_ready():
        if not getattr(bot, '_hr_views_registered', False):
            bot.add_view(ApplicationPanelView())
            bot.add_view(ApprovalView())
            bot.add_view(ReinstateView())
            bot.add_view(NewTicketPanelView())
            bot._hr_views_registered = True

        if not getattr(bot, '_hr_reinstate_checker', None):
            bot._hr_reinstate_checker = ReinstateChecker(bot)

        if not getattr(bot, '_hr_channels_audited', False):
            bot._hr_channels_audited = True
            bot.loop.create_task(audit_created_channels_on_startup(bot))

        if not getattr(bot, '_new_tickets_audited', False):
            bot._new_tickets_audited = True
            bot.loop.create_task(audit_new_tickets_on_startup(bot))

        try:
            await bot.tree.sync()
        except Exception as e:
            print(f"❌ [HR] スラッシュコマンドの同期に失敗しました: {e}")

    @bot.listen('on_message')
    async def on_new_ticket_intake_message(message: discord.Message):
        """チケットチャンネル内でのメッセージから、作成難易度・運転区間などのフォーマット項目を自動読み取りする"""
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if not is_new_ticket_channel(message.channel):
            return

        updates = parse_ticket_format_message(message.content)
        if not updates:
            return

        data = await update_ticket_channel_topic(message.channel, **updates)
        if data.get("log_msg"):
            await send_or_update_ticket_log(bot, message.channel, data)

    @bot.tree.command(name="panel", description="各種申請パネルを設置します")
    async def panel_command(interaction: discord.Interaction):
        if not is_admin_role_or_higher(interaction.user):
            await interaction.response.send_message("⚠️ このコマンドを実行する権限がありません。", ephemeral=True)
            return
        await interaction.response.send_message(embed=generate_application_panel_embed(), view=ApplicationPanelView())

    @bot.command(name="recommendadminpanel")
    async def recommend_admin_panel(ctx: commands.Context):
        if not is_admin_role_or_higher(ctx.author):
            await ctx.send("❌ このコマンドを実行する権限がありません。")
            return
        embed = generate_admin_embed()
        view = AdminPanelEditView(bot)
        await ctx.send(embed=embed, view=view)

    @bot.command(name="testsave")
    async def test_save_cmd(ctx: commands.Context):
        """データ保存のテストを行う管理者用コマンド"""
        if not is_admin_role_or_higher(ctx.author):
            await ctx.send("❌ このコマンドを実行する権限がありません。")
            return
        await ctx.send("💾 テスト保存を実行します...")
        await save_config_to_discord(ctx.bot)
        await ctx.send("✅ テスト保存処理が完了しました。")

    @bot.command(name="close")
    async def close_cmd(ctx: commands.Context):
        """!close コマンドで確認なしですぐにチャンネルをクローズして削除する"""
        await execute_channel_close(ctx.channel, ctx.author, ctx.bot)

    @bot.command(name="sendmessage")
    async def send_message_cmd(ctx: commands.Context, channel: discord.TextChannel, *, message: str):
        """指定チャンネルにメッセージを送信する管理者用コマンド"""
        if not is_admin_role_or_higher(ctx.author):
            await ctx.send("❌ このコマンドを実行する権限がありません。")
            return

        try:
            await channel.send(message)
            await ctx.send(f"✅ {channel.mention} にメッセージを送信しました。")
        except Exception as e:
            await ctx.send(f"❌ メッセージの送信に失敗しました: {e}")

    # ==========================================
    # 【追加】運転整理チケットBOT機能 コマンド群
    # ==========================================
    @bot.command(name="sendpanel")
    async def sendpanel_cmd(ctx: commands.Context):
        """チケット作成用のパネルを設置する"""
        if not can_view_new_tickets(ctx.author):
            await ctx.send("❌ このコマンドを実行する権限がありません。")
            return
        await ctx.send(embed=generate_new_ticket_panel_embed(), view=NewTicketPanelView())

    @bot.command(name="assign")
    async def assign_cmd(ctx: commands.Context, member: discord.Member = None):
        """担当者の設定・変更（メンションなしで自分、ありで指定した人物）"""
        if not is_new_ticket_channel(ctx.channel):
            return
        if not can_view_new_tickets(ctx.author):
            try:
                await ctx.message.delete()
            except Exception:
                pass
            return

        target = member or ctx.author

        try:
            await ctx.message.delete()
        except Exception:
            pass

        data = await update_ticket_channel_topic(ctx.channel, assignee=target.id)
        await refresh_ticket_channel_name(ctx.channel, data, finished=False)

        log_msg_id = await send_or_update_ticket_log(ctx.bot, ctx.channel, data)
        if log_msg_id:
            data = await update_ticket_channel_topic(ctx.channel, log_msg=log_msg_id)

        await ctx.send(f"{target.mention} さんが担当者になりました。")

    @bot.command(name="adduser")
    async def adduser_cmd(ctx: commands.Context, member: discord.Member = None):
        """チケットチャンネルに指定したユーザーを追加（閲覧権限を付与）する"""
        if not is_new_ticket_channel(ctx.channel):
            return
        if not can_view_new_tickets(ctx.author):
            await ctx.send("❌ このコマンドを実行する権限がありません。")
            return
        if member is None:
            await ctx.send("⚠️ 追加するユーザーをメンションしてください。例: `!adduser @ユーザー`")
            return

        try:
            await ctx.channel.set_permissions(member, read_messages=True, send_messages=True)
        except Exception as e:
            await ctx.send(f"❌ 権限付与に失敗しました: {e}")
            return

        await ctx.send(f"✅ {member.mention} さんをこのチケットに追加しました。")

    @bot.command(name="change")
    async def change_cmd(ctx: commands.Context, member: discord.Member = None):
        """現在担当中の人（または特権ID・ロール持ち）が使用可能。担当者変更とチャンネル名更新を行う"""
        if not is_new_ticket_channel(ctx.channel):
            return

        data = parse_ticket_topic(ctx.channel.topic) or {}
        current_assignee_id = data.get("assignee")
        is_current_assignee = bool(current_assignee_id) and str(ctx.author.id) == current_assignee_id
        if not (is_current_assignee or has_ticket_override(ctx.author)):
            await ctx.send("❌ 現在の担当者、または特権を持つユーザーのみ実行できます。")
            return
        if member is None:
            await ctx.send("⚠️ 新しい担当者をメンションしてください。例: `!change @ユーザー`")
            return

        data = await update_ticket_channel_topic(ctx.channel, assignee=member.id)
        await refresh_ticket_channel_name(ctx.channel, data, finished=False)

        log_msg_id = await send_or_update_ticket_log(ctx.bot, ctx.channel, data)
        if log_msg_id:
            data = await update_ticket_channel_topic(ctx.channel, log_msg=log_msg_id)

        await ctx.send(f"✅ 担当者を {member.mention} さんに変更しました。")

    @bot.command(name="finish")
    async def finish_cmd(ctx: commands.Context, *, reason: str = "理由未記入"):
        """担当終了を意味し、チャンネル名が finished-ticket-◯◯◯◯ に変わり対応終了日が更新される"""
        if not is_new_ticket_channel(ctx.channel):
            return

        data = parse_ticket_topic(ctx.channel.topic) or {}
        current_assignee_id = data.get("assignee")
        is_current_assignee = bool(current_assignee_id) and str(ctx.author.id) == current_assignee_id
        if not (is_current_assignee or has_ticket_override(ctx.author)):
            await ctx.send("❌ 現在の担当者、または特権を持つユーザーのみ実行できます。")
            return

        finished_date = datetime.datetime.now(JST).strftime("%Y/%m/%d")
        data = await update_ticket_channel_topic(ctx.channel, finished=finished_date)
        await refresh_ticket_channel_name(ctx.channel, data, finished=True)

        log_msg_id = await send_or_update_ticket_log(ctx.bot, ctx.channel, data)
        if log_msg_id:
            data = await update_ticket_channel_topic(ctx.channel, log_msg=log_msg_id)

        await ctx.send(f"🏁 対応を終了しました。\n**理由:** {reason}")

    @bot.command(name="jobs")
    async def jobs_cmd(ctx: commands.Context):
        """現在担当中のチケットを一覧表示する"""
        guild = ctx.guild
        category = await get_new_ticket_category(guild)
        if category is None:
            await ctx.send("❌ チケットカテゴリーが見つかりませんでした。")
            return

        mine = []
        for ch in category.channels:
            data = parse_ticket_topic(ch.topic)
            if not data or data.get("finished"):
                continue
            if data.get("assignee") == str(ctx.author.id):
                mine.append((ch, data))

        if not mine:
            await ctx.send("現在担当中のチケットはありません。")
            return

        embed = discord.Embed(title=f"📂 {ctx.author.display_name} さんの担当中チケット", color=discord.Color.blue())
        for ch, data in mine:
            embed.add_field(
                name=f"#{data.get('number', '----')} {ch.name}",
                value=f"運転区間: {data.get('section') or '未設定'}\n依頼日: {data.get('requested') or '不明'}",
                inline=False,
            )
        await ctx.send(embed=embed)

    @bot.command(name="DMannounce")
    async def dm_announce_cmd(ctx: commands.Context, *, message: str = None):
        """依頼者のDMへBot経由で案内を送る"""
        if not is_new_ticket_channel(ctx.channel):
            await ctx.send("❌ このコマンドはチケットチャンネル内でのみ使用できます。")
            return
        if not can_view_new_tickets(ctx.author):
            await ctx.send("❌ このコマンドを実行する権限がありません。")
            return
        if not message:
            await ctx.send("⚠️ 送信するメッセージを指定してください。例: `!DMannounce ご確認をお願いします。`")
            return

        data = parse_ticket_topic(ctx.channel.topic) or {}
        requester_id = data.get("requester")
        if not requester_id:
            await ctx.send("❌ 依頼者情報が見つかりませんでした。")
            return

        guild = ctx.guild
        member = guild.get_member(int(requester_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(requester_id))
            except Exception:
                member = None
        if member is None:
            await ctx.send("❌ 依頼者が見つかりませんでした。")
            return

        try:
            dm_embed = discord.Embed(
                title=f"🎫 チケット #{data.get('number', '----')} からのお知らせ",
                description=message,
                color=discord.Color.blurple(),
                timestamp=datetime.datetime.now(JST),
            )
            dm_embed.set_footer(text=ctx.guild.name)
            await member.send(embed=dm_embed)
        except Exception as e:
            await ctx.send(f"❌ DM送信に失敗しました（相手がDMを許可していない可能性があります）: {e}")
            return

        await ctx.send(f"✅ {member.mention} さんへDMを送信しました。")
