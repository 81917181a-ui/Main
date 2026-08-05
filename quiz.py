import discord
from discord.ext import commands
import json
import os
import asyncio
import io

# ==========================================
# 永続化用チャンネル・メッセージ・ロール管理
# ==========================================
CONFIG_CHANNEL_ID = 1526289865719943329   # 設定保存用チャンネルID
LOG_CHANNEL_ID = 1510042822533840936     # ログ送信先チャンネルID
ADMIN_ROLE_ID = 1510405214811852900      # 基準となる管理者ロールID
PANEL_AUTO_SEND_CHANNEL_ID = 1531834782881808566  # 再起動時自動送信・更新先チャンネルID

config_message_id = None

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

async def execute_channel_close(interaction: discord.Interaction):
    """チャンネル削除処理およびログ保存の共通関数"""
    bot = interaction.client
    channel = interaction.channel

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            print(f"❌ ログ送信チャンネルの取得に失敗しました: {e}")

    messages = []
    async for msg in channel.history(limit=500, oldest_first=True):
        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        content = msg.content if msg.content else "[埋め込み/メディアメッセージ]"
        messages.append(f"[{timestamp}] {msg.author.display_name} ({msg.author.id}): {content}")

    history_text = "\n".join(messages)
    file_data = io.BytesIO(history_text.encode("utf-8"))
    discord_file = discord.File(file_data, filename=f"history-{channel.name}.txt")

    log_embed = discord.Embed(
        title="🔒 応募チャンネルクローズドログ",
        description=f"**対象チャンネル:** `{channel.name}`\n**実行者:** {interaction.user.mention} (`{interaction.user.id}`)",
        color=discord.Color.red()
    )

    if log_channel:
        await log_channel.send(embed=log_embed, file=discord_file)

    await asyncio.sleep(2)
    await channel.delete(reason="応募チャンネル閉鎖のため")

class CloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="はい (クローズする)", style=discord.ButtonStyle.danger, custom_id="quiz_close_confirm_yes")
    async def confirm_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 チャンネルをクローズして会話履歴をログチャンネルへ送信しています...", ephemeral=True)
        await execute_channel_close(interaction)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary, custom_id="quiz_close_confirm_no")
    async def confirm_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ クローズ処理をキャンセルしました。", embed=None, view=None)

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
                description="30分間回答がなかったため、質問セッションを終了しました。\n再度やり直す場合は `!closereq` でチャンネルをクローズしてください。",
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
        joined_at_str = user.joined_at.strftime("%Y/%m/%d %H:%M:%S") if user.joined_at else "不明"

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
        embed = discord.Embed(
            title="✨ 自己推薦・応募受付",
            description="下のメニューから応募したい部署を選択してください。",
            color=discord.Color.green()
        )
        await interaction.channel.send(embed=embed, view=QuizUserPanelView())
        await interaction.response.send_message("✅ このチャンネルに応募用パネルを送信しました！", ephemeral=True)

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

# 👇 *args, **kwargs を使って複数の引数を受け取れるように変更
def setup_quiz_commands(bot: commands.Bot, *args, **kwargs):
    @bot.listen('on_ready')
    async def on_quiz_ready():
        if not hasattr(bot, '_quiz_auto_send_task') or bot._quiz_auto_send_task.done():
            bot._quiz_auto_send_task = bot.loop.create_task(auto_send_user_panel(bot))

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

    @bot.command(name="closereq")
    async def close_req_cmd(ctx: commands.Context):
        """!closereq コマンドでクローズ確認メッセージを表示"""
        embed = discord.Embed(
            title="🔒 チャンネルの削除確認",
            description="本当にこのチャンネルをクローズ（削除）しますか？\n会話履歴のテキストファイルがログチャンネルに送られます。",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed, view=CloseConfirmView())

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
