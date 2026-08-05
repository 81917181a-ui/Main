from datetime import datetime, timedelta
import json
import os
import random
import threading
import time
import asyncio
import sys
from openai import OpenAI
import requests  # ★セルフ・ピン用に追加

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# 🛠️ 新しいシステム（sub.py）をインポート
import sub

# 🛠️ 自己推薦システム（Quiz.py）をインポート
from quiz import load_config_from_discord, setup_quiz_commands

# ==========================================
# Flask (RenderやUptimeRobot等の死活監視用)
# ==========================================
try:
    from flask import Flask
except ModuleNotFoundError:
    class Flask:
        def __init__(self, name):
            self.name = name
            self.routes = {}

        def route(self, path):
            def decorator(func):
                self.routes[path] = func
                return func
            return decorator

        def run(self, *args, **kwargs):
            pass
    print("警告: flask がインストールされていないため、ダミーを使用します。")

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

# Renderのヘルスチェック用に追加
@app.route("/health")
def health():
    return "OK", 200

def run_flask():
    # Renderが指定するポート（環境変数 PORT）またはデフォルトで8080を使う
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

# ==========================================
# ★追加：自分自身を定期的に叩いてスリープを防ぐ機能
# ==========================================
def self_ping():
    # あなたのRenderのURL
    url = "https://mainbot-tuwm.onrender.com/health"
    
    while True:
        try:
            # 10分ごとに自分にアクセス（15分でスリープするのを防ぎます）
            time.sleep(600)
            response = requests.get(url)
            print(f"Self-ping sent! Status: {response.status_code}")
        except Exception as e:
            print(f"Self-ping failed: {e}")

def start_ping_thread():
    p = threading.Thread(target=self_ping, daemon=True)
    p.start()

# サーバーとセルフ・ピンを起動
keep_alive()
start_ping_thread()

# ==========================================
# Discord
# ==========================================
import discord
from discord.ext import commands
from discord import app_commands

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# 🛠️ Quiz.py のテキストコマンド (!recommendadminpanel, !recommendpanel) をBotに登録
setup_quiz_commands(bot)

# ==========================================
# JSON 永続化データ管理
# ==========================================
DATA_FILE = "trains.json"
USAGE_FILE = "usage.json"

def load_trains():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_trains(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def load_usage_data():
    if not os.path.exists(USAGE_FILE):
        return {}
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def save_usage_data(data):
    try:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except OSError:
        pass

# ==========================================
# 権限・クールダウン設定
# ==========================================
COMMAND_ROLE_ID = 1510405214811852900
ADMIN_ROLE_ID = 1510021467167789104
DEFAULT_ROUTE_NAME = "尾羽急本線"

COMMAND_RULES = {
    "create": {
        "unlimited_roles": [1511882841997185064],
        "limited_roles": [1510021467155202057],
        "window_sec": 12 * 3600,
        "limit": 4
    },
    "create_emp": {
        "unlimited_roles": [1511882841997185064],
        "limited_roles": [1510021467167789097, 1511882841997185064],
        "window_sec": 24 * 3600,
        "limit": 5
    },
    "create_man": {
        "unlimited_roles": [1511882841997185064],
        "limited_roles": [1510405214811852900],
        "window_sec": 24 * 3600,
        "limit": 4
    },
    "create_auto": {
        "unlimited_roles": [1511882841997185064],
        "limited_roles": [1510405214811852900],
        "window_sec": 24 * 3600,
        "limit": 4
    }
}

def format_remaining_time(seconds):
    if seconds <= 0:
        return "0時間0分"
    hours = seconds // 3600
    minutes = (seconds % 3600 + 59) // 60
    if minutes >= 60:
        hours += 1
        minutes -= 60
    return f"{hours}時間{minutes}分"

async def check_command_permission(interaction: discord.Interaction, command_name: str) -> bool:
    rules = COMMAND_RULES.get(command_name)
    if rules is None:
        await interaction.response.send_message("このコマンドを使用する権限がありません。", ephemeral=True)
        return False

    user_roles = [getattr(role, "id", None) for role in getattr(interaction.user, "roles", [])]
    if ADMIN_ROLE_ID in user_roles:
        return True
    if any(role_id in rules["unlimited_roles"] for role_id in user_roles):
        return True
    if not any(role_id in rules["limited_roles"] for role_id in user_roles):
        await interaction.response.send_message("このコマンドを使用する権限がありません。", ephemeral=True)
        return False

    user_id = str(interaction.user.id)
    now = int(time.time())
    usage_data = load_usage_data()
    user_usage = usage_data.setdefault(user_id, {})
    timestamps = user_usage.setdefault(command_name, [])
    window_start = now - rules["window_sec"]
    timestamps = [ts for ts in timestamps if ts > window_start]

    if len(timestamps) >= rules["limit"]:
        earliest = min(timestamps)
        remaining = earliest + rules["window_sec"] - now
        await interaction.response.send_message(f"🚫 クールダウン中です。\n\nあと **{format_remaining_time(remaining)}** 後に利用できます。\n💡 `/quiz` に正解すると、制限をリセットできるチャンスがあります！", ephemeral=True)
        user_usage[command_name] = timestamps
        save_usage_data(usage_data)
        return False

    timestamps.append(now)
    user_usage[command_name] = timestamps
    usage_data[user_id] = user_usage
    save_usage_data(usage_data)
    return True

# ==========================================
# 鉄道データ (路線・駅・所要時間)
# ==========================================
ROUTE_STATIONS = {
    "尾羽急本線": ["尾羽原", "井口", "梅郷", "雲中", "安越", "十川", "千峰", "南雲谷", "雲谷", "長峰", "西高徳", "高徳", "明神前", "舞子台", "紀田", "穂", "瀬舞", "余美", "千鳥"],
    "空港線": ["尾羽原", "井口", "梅郷", "雲中", "安越", "十川", "千峰", "南雲谷", "雲谷", "長峰", "西高徳", "高徳", "新高徳", "整備場", "空港"],
    "井問線": ["安越", "雲中", "梅郷", "井口", "上井口", "参田町", "東本郷", "本郷", "西問屋町", "問屋町", "千鳥"]
}

STOP_TIME = 30
TURNBACK_MINUTES = 5
MIN_HEADWAY = 2

LOCAL = {("尾羽原", "井口"): 60, ("井口", "梅郷"): 60, ("梅郷", "雲中"): 30, ("雲中", "安越"): 60, ("安越", "十川"): 60, ("十川", "千峰"): 90, ("千峰", "南雲谷"): 80, ("南雲谷", "雲谷"): 120, ("雲谷", "長峰"): 75, ("長峰", "西高徳"): 90, ("西高徳", "高徳"): 90, ("高徳", "明神前"): 120, ("明神前", "舞子台"): 100, ("舞子台", "紀田"): 100, ("紀田", "穂"): 90, ("穂", "瀬舞"): 90, ("瀬舞", "余美"): 90, ("余美", "千鳥"): 90}
EXPRESS = {("尾羽原", "井口"): 85, ("井口", "安越"): 85, ("安越", "雲谷"): 180, ("雲谷", "高徳"): 140, ("高徳", "紀田"): 180, ("紀田", "千鳥"): 120}
SEMI_EXPRESS = {("尾羽原", "井口"): 90, ("井口", "雲中"): 120, ("雲中", "安越"): 60, ("安越", "千峰"): 120, ("千峰", "雲谷"): 150, ("雲谷", "長峰"): 120, ("長峰", "高徳"): 150, ("高徳", "舞子台"): 180, ("舞子台", "紀田"): 120, ("紀田", "千鳥"): 180}
RAPID = {("尾羽原", "井口"): 60, ("井口", "雲中"): 90, ("雲中", "安越"): 60, ("安越", "千峰"): 120, ("千峰", "雲谷"): 180, ("雲谷", "長峰"): 75, ("長峰", "西高徳"): 90, ("西高徳", "高徳"): 90, ("高徳", "明神前"): 120, ("明神前", "舞子台"): 100, ("舞子台", "紀田"): 100, ("紀田", "穂"): 90, ("穂", "瀬舞"): 90, ("瀬舞", "余美"): 90, ("余美", "千鳥"): 90}
RAPID_EXPRESS = {("尾羽原", "高徳"): 410, ("高徳", "紀田"): 180, ("紀田", "千鳥"): 120}
IDEMON_LOCAL = {("安越", "雲中"): 60, ("雲中", "梅郷"): 60, ("梅郷", "井口"): 60, ("井口", "上井口"): 90, ("上井口", "参田町"): 90, ("参田町", "東本郷"): 60, ("東本郷", "本郷"): 60, ("本郷", "西問屋町"): 80, ("西問屋町", "問屋町"): 110, ("問屋町", "千鳥"): 110}
IDEMON_RAPID = {("安越", "雲中"): 60, ("雲中", "井口"): 90, ("井口", "参田町"): 150, ("参田町", "本郷"): 90, ("本郷", "問屋町"): 180, ("問屋町", "千鳥"): 110}
IDEMON_EXPRESS = {("安越", "井口"): 120, ("井口", "本郷"): 240, ("本郷", "千鳥"): 240}
SECTION_EXPRESS = {("尾羽原", "井口"): 90, ("井口", "安越"): 120, ("安越", "雲谷"): 210, ("雲谷", "高徳"): 180, ("高徳", "明神前"): 150, ("明神前", "舞子台"): 120, ("舞子台", "紀田"): 90, ("紀田", "穂"): 90, ("穂", "瀬舞"): 90, ("瀬舞", "余美"): 90, ("余美", "千鳥"): 90}

ROUTE_TRAIN_TYPES = {
    "尾羽急本線": {"普通": LOCAL, "準急": SEMI_EXPRESS, "区間急行": SECTION_EXPRESS, "快速": RAPID, "急行": EXPRESS, "快速急行": RAPID_EXPRESS},
    "空港線": {
        "普通": {("尾羽原", "井口"): 60, ("井口", "梅郷"): 60, ("梅郷", "雲中"): 30, ("雲中", "安越"): 60, ("安越", "十川"): 60, ("十川", "千峰"): 90, ("千峰", "南雲谷"): 80, ("南雲谷", "雲谷"): 120, ("雲谷", "長峰"): 75, ("長峰", "西高徳"): 90, ("西高徳", "高徳"): 90, ("高徳", "新高徳"): 120, ("新高徳", "整備場"): 120, ("整備場", "空港"): 120},
        "快速": {("尾羽原", "井口"): 60, ("井口", "雲中"): 90, ("雲中", "安越"): 60, ("安越", "千峰"): 120, ("千峰", "南雲谷"): 180, ("南雲谷", "雲谷"): 180, ("雲谷", "長峰"): 75, ("長峰", "西高徳"): 90, ("西高徳", "高徳"): 90, ("高徳", "新高徳"): 120, ("新高徳", "整備場"): 120, ("整備場", "空港"): 120},
        "空港急行": {("尾羽原", "高徳"): 410, ("高徳", "新高徳"): 90, ("新高徳", "整備場"): 90, ("整備場", "空港"): 90}
    },
    "井問線": {"普通": IDEMON_LOCAL, "快速": IDEMON_RAPID, "急行": IDEMON_EXPRESS}
}

# ==========================================
# クイズデータ (尾羽急鉄道クイズ)
# ==========================================
QUIZ_LIST = [
    {
        "question": "尾羽急本線の「快速急行」が、尾羽原を出発した後に最初に停車する駅はどこ？",
        "choices": ["井口", "安越", "高徳", "千峰"],
        "answer": "高徳"
    },
    {
        "question": "空港線の終着駅はどこ？",
        "choices": ["空港", "新高徳", "整備場", "千鳥"],
        "answer": "空港"
    },
    {
        "question": "井問線の「普通」列車で、安越から雲中までの所要時間は何秒？",
        "choices": ["30秒", "60秒", "90秒", "120秒"],
        "answer": "60秒"
    },
    {
        "question": "尾羽急本線において、区間急行が停車しない駅は次のうちどれ？",
        "choices": ["南雲谷", "舞子台", "千鳥", "紀田"],
        "answer": "南雲谷"
    }
]

# ==========================================
# ダイヤ計算ロジック
# ==========================================
def get_stops(route_name, train_type):
    data = ROUTE_TRAIN_TYPES[route_name][train_type]
    stations = set()
    for a, b in data.keys():
        stations.add(a)
        stations.add(b)
    return stations

def calculate_times(route_name, train_type, start_station, end_station, start_time_str):
    if route_name not in ROUTE_STATIONS:
        return None, "未実装の路線です"
    if train_type not in ROUTE_TRAIN_TYPES[route_name]:
        return None, "その種別はありません"

    stations = ROUTE_STATIONS[route_name]
    if start_station not in stations: 
        return None, "開始駅が存在しません"
    if end_station not in stations: 
        return None, "終了駅が存在しません"

    stops = get_stops(route_name, train_type)
    if start_station not in stops: 
        return None, "開始駅は停車駅ではありません"
    if end_station not in stops: 
        return None, "終了駅は停車駅ではありません"

    try:
        base_time = datetime.strptime(start_time_str, "%H:%M")
    except ValueError:
        return None, "時刻の形式が不正です (HH:MM で指定してください)"

    start_idx = stations.index(start_station)
    end_idx = stations.index(end_station)
    is_down = start_idx < end_idx

    current_idx = start_idx
    current_time = base_time
    timetable = []

    timetable.append({
        "station": start_station,
        "arr": "--:--",
        "dep": current_time.strftime("%H:%M")
    })

    route_data = ROUTE_TRAIN_TYPES[route_name][train_type]

    while current_idx != end_idx:
        next_idx = current_idx + 1 if is_down else current_idx - 1
        curr_st = stations[current_idx]
        next_st = stations[next_idx]

        section = (curr_st, next_st) if is_down else (next_st, curr_st)
        if section in route_data:
            travel_time_sec = route_data[section]
            current_time += timedelta(seconds=travel_time_sec)
            
            if next_st in stops:
                arr_time = current_time
                if next_idx == end_idx:
                    timetable.append({
                        "station": next_st,
                        "arr": arr_time.strftime("%H:%M"),
                        "dep": "--:--"
                    })
                else:
                    dep_time = arr_time + timedelta(seconds=STOP_TIME)
                    timetable.append({
                        "station": next_st,
                        "arr": arr_time.strftime("%H:%M"),
                        "dep": dep_time.strftime("%H:%M")
                    })
                    current_time = dep_time
        
        current_idx = next_idx

    return timetable, None

# ==========================================
# クイズ UI (インタラクティブボタン)
# ==========================================
class QuizView(discord.ui.View):
    def __init__(self, quiz_data, user_id):
        super().__init__(timeout=60.0)
        self.quiz_data = quiz_data
        self.user_id = user_id

        for choice in self.quiz_data["choices"]:
            button = discord.ui.Button(label=choice, style=discord.ButtonStyle.primary, custom_id=choice)
            button.callback = self.on_button_click
            self.add_item(button)

    async def on_button_click(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("これはあなたのクイズではありません！", ephemeral=True)
            return

        selected_answer = interaction.data["custom_id"]
        correct_answer = self.quiz_data["answer"]

        for item in self.children:
            item.disabled = True

        if selected_answer == correct_answer:
            usage_data = load_usage_data()
            user_id_str = str(interaction.user.id)
            if user_id_str in usage_data:
                if "create" in usage_data[user_id_str]:
                    usage_data[user_id_str]["create"] = []
                save_usage_data(usage_data)
                benefit_text = "\n🎉 **ご褒美:** `/create` のクールダウン制限が完全にリセットされました！今すぐ作成可能です！"
            else:
                benefit_text = "\n🎉 **ご褒美:** クールダウンは元々ありませんが、これでいつでも作成可能です！"

            embed = discord.Embed(
                title="🟢 正解！",
                description=f"お見事！正解は **{correct_answer}** です！{benefit_text}",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="🔴 不正解...",
                description=f"残念！正解は **{correct_answer}** でした。また次回挑戦してください！",
                color=discord.Color.red()
            )

        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()
# ==========================================
# 🐺 人狼ゲーム システム（DMテキスト入力版・占い師追加・襲撃失敗追加・勝者表示対応）
# ==========================================

active_games = {} # { channel_id: GameSession }

class WolfLobbyView(discord.ui.View):
    def __init__(self, host):
        super().__init__(timeout=300.0)
        self.host = host
        self.joined = [host]

    def get_embed(self):
        return discord.Embed(
            title="🐺 人狼ゲーム 参加者募集中！",
            description=f"**ホスト:** {self.host.mention}\n**現在の参加者 ({len(self.joined)}人):**\n" + "\n".join([f"- {p.mention}" for p in self.joined]),
            color=discord.Color.blue()
        )

    @discord.ui.button(label="参加する", style=discord.ButtonStyle.success, custom_id="wolf_join")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user in self.joined:
            await interaction.response.send_message("すでに参加しています！", ephemeral=True)
            return
        self.joined.append(interaction.user)
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="スタート", style=discord.ButtonStyle.primary, custom_id="wolf_start")
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.host:
            await interaction.response.send_message("ホストのみがスタートできます！", ephemeral=True)
            return
        if len(self.joined) < 4:
            await interaction.response.send_message("占い師を追加するため、最低4人必要です！", ephemeral=True)
            return
        
        for item in self.children:
            item.disabled = True
        
        start_embed = discord.Embed(
            title="🐺 人狼ゲームが開始されました！",
            description=f"参加者: {', '.join([p.mention for p in self.joined])}\n\n各プレイヤーの **DM** に役職を送信しました。確認してください！",
            color=discord.Color.dark_purple()
        )
        await interaction.response.edit_message(embed=start_embed, view=self)
        self.stop()

        session = WolfGameSession(interaction.channel, self.joined, self.host, interaction.client)
        active_games[interaction.channel.id] = session
        asyncio.create_task(session.run_game_loop())

class WolfGameSession:
    def __init__(self, channel, players, host, bot):
        self.channel = channel
        self.players = players
        self.host = host
        self.bot = bot
        self.is_running = True
        
        roles_list = ["🐺 人狼", "🔮 占い師"] + ["🧑‍🌾 村人"] * (len(players) - 2)
        random.shuffle(roles_list)
        self.roles = dict(zip(players, roles_list))
        self.alive = list(players)
        self.day_count = 0

    def get_wolf_player(self):
        for p, role in self.roles.items():
            if role == "🐺 人狼":
                return p
        return None

    def get_seer_player(self):
        for p, role in self.roles.items():
            if role == "🔮 占い師":
                return p
        return None

    async def send_roles(self):
        for p, role in self.roles.items():
            try:
                await p.send(f"🔒 【役職通知】\n今回のあなたの役職は【 **{role}** 】です！この内容は他の人には秘密にしてください。")
            except:
                await self.channel.send(f"{p.mention} さんのDMが閉じているため役職を送信できませんでした！設定を確認してください。", delete_after=10)

    async def get_text_input(self, user, prompt_text, valid_targets):
        """DMでメッセージ入力を受け付け、有効な対象プレイヤーを返す（制限時間なし）"""
        try:
            await user.send(prompt_text)
        except Exception:
            return None

        def check(m):
            if m.author != user or not isinstance(m.channel, discord.DMChannel):
                return False
            content = m.content.strip()
            for p in valid_targets:
                if (content == p.name or 
                    content == p.global_name or 
                    content == p.display_name or 
                    content == f"<@{p.id}>" or 
                    content == str(p.id)):
                    return True
            return False

        while self.is_running:
            try:
                msg = await self.bot.wait_for('message', check=check)
                content = msg.content.strip()
                for p in valid_targets:
                    if (content == p.name or 
                        content == p.global_name or 
                        content == p.display_name or 
                        content == f"<@{p.id}>" or 
                        content == str(p.id)):
                        return p
            except Exception:
                break
        return None

    async def run_game_loop(self):
        await self.send_roles()

        await self.channel.send(embed=discord.Embed(
            title="🔒 役職の配布が完了しました",
            description="全員のDMに役職を送信しました。これより夜のフェーズが始まります！",
            color=discord.Color.dark_purple()
        ))

        while self.is_running:
            self.day_count += 1
            
            # --- 🌙 夜のフェーズ ---
            wolves = [p for p in self.alive if self.roles[p] == "🐺 人狼"]
            seer = self.get_seer_player()
            
            night_embed = discord.Embed(
                title=f"🌙 第{self.day_count}日目 - 夜が訪れました",
                description="村に暗闇が包み込みました...\n人狼と占い師はそれぞれの行動をDMで行ってください。",
                color=discord.Color.dark_purple()
            )
            await self.channel.send(embed=night_embed)

            # 1. 占い師の行動
            if seer and seer in self.alive:
                seer_valid_targets = [p for p in self.alive if p != seer]
                if seer_valid_targets:
                    target_list_str = "\n".join([f"- {p.name} (表示名: {p.display_name})" for p in seer_valid_targets])
                    prompt = (
                        f"🔮 **【占い師の予言】**\n"
                        f"今夜、誰の正体を知りたいですか？対象の**ユーザーネーム**（または表示名）をDMに送信してください：\n\n"
                        f"**【生存者一覧】**\n{target_list_str}"
                    )
                    seer_target = await self.get_text_input(seer, prompt, seer_valid_targets)
                    if not self.is_running: break
                    
                    if seer_target:
                        target_role = self.roles[seer_target]
                        try:
                            await seer.send(f"🔮 【結果通知】\n**{seer_target.display_name}** の正体は 【 **{target_role}** 】 です。")
                        except:
                            pass

            # 2. 人狼の襲撃
            killed_target = None
            attack_failed = False
            valid_targets = [p for p in self.alive if self.roles[p] != "🐺 人狼"]
            if not valid_targets:
                valid_targets = self.alive

            for wolf in wolves:
                if wolf not in self.alive:
                    continue
                
                target_list_str = "\n".join([f"- {p.name} (表示名: {p.display_name})" for p in valid_targets])
                prompt = (
                    f"🐺 **【人狼の夜襲】**\n"
                    f"今夜襲撃する相手の**ユーザーネーム**（または表示名）をDMに送信してください：\n\n"
                    f"**【生存者一覧】**\n{target_list_str}"
                )
                
                target = await self.get_text_input(wolf, prompt, valid_targets)
                if not self.is_running: break
                
                if target:
                    if random.random() < 0.25:
                        attack_failed = True
                    else:
                        killed_target = target
                        
                    try:
                        await wolf.send(f"✅ 【 **{target.display_name}** 】への襲撃を受け付けました。")
                    except:
                        pass
                    break

            if not self.is_running: break

            if not killed_target and not attack_failed and self.alive:
                killed_target = random.choice(valid_targets or self.alive)

            # --- ☀️ 朝のフェーズ ---
            if attack_failed:
                morning_embed = discord.Embed(
                    title=f"☀️ 第{self.day_count}日目 - 朝が来ました",
                    description="昨夜、人狼が襲撃を試みましたが、ターゲットが反撃して人狼が致命傷を負いました…！\n\n本日の犠牲者はいません（襲撃失敗）。",
                    color=discord.Color.orange()
                )
                await self.channel.send(embed=morning_embed)
            else:
                if killed_target in self.alive:
                    self.alive.remove(killed_target)

                morning_embed = discord.Embed(
                    title=f"☀️ 第{self.day_count}日目 - 朝が来ました",
                    description=f"昨夜の犠牲者が発見されました...\n\n惨たらしい姿で発見されたのは **{killed_target.mention}** さんでした。",
                    color=discord.Color.orange()
                )
                await self.channel.send(content=killed_target.mention, embed=morning_embed)

            wolf_player = self.get_wolf_player()
            seer_player = self.get_seer_player()
            alive_wolves = [p for p in self.alive if self.roles[p] == "🐺 人狼"]

            if len(self.alive) == 2 and len(alive_wolves) == 1:
                seer_text = f"今回の占い師は **{seer_player.mention}** でした！" if seer_player else ""
                await self.channel.send(
                    f"🐺 **人狼陣営の勝利！**\n"
                    f"生き残ったのは人狼（{alive_wolves[0].mention}）と村人1人のみになりました！\n"
                    f"今回の人狼は **{wolf_player.mention}** でした！\n"
                    f"{seer_text}"
                )
                break

            if not alive_wolves:
                seer_text = f"今回の占い師は **{seer_player.mention}** でした！" if seer_player else ""
                await self.channel.send(
                    f"🎉 **村人陣営の勝利！**\n"
                    f"人狼が排除されました！\n"
                    f"今回の人狼は **{wolf_player.mention}** でした！\n"
                    f"{seer_text}"
                )
                break

            if not self.is_running: break

            # --- 🗣️ 昼の議論 ＆ ⚖️ 投票フェーズ ---
            disc_embed = discord.Embed(
                title="🗣️ 昼の議論タイム",
                description="生き残ったメンバーで自由に話し合い、誰が人狼か推理してください。\n（順番に各プレイヤーのDMに処刑投票用の案内が届きます）",
                color=discord.Color.green()
            )
            await self.channel.send(embed=disc_embed)

            votes = {}
            for voter in list(self.alive):
                valid_targets = [p for p in self.alive if p != voter]
                target_list_str = "\n".join([f"- {p.name} (表示名: {p.display_name})" for p in valid_targets])
                
                prompt = (
                    f"⚖️ **【処刑投票】**\n"
                    f"本日処刑するプレイヤーの**ユーザーネーム**（または表示名）をDMに送信してください：\n\n"
                    f"**【投票先候補】**\n{target_list_str}"
                )
                
                target = await self.get_text_input(voter, prompt, valid_targets)
                if not self.is_running: break
                
                if target:
                    votes[voter] = target
                    try:
                        await voter.send(f"✅ 【 **{target.display_name}** 】への投票を受け付けました。")
                    except:
                        pass

            if not self.is_running: break

            if votes:
                vote_counts = {}
                for target in votes.values():
                    vote_counts[target] = vote_counts.get(target, 0) + 1
                
                executed_target = max(vote_counts, key=vote_counts.get)
                if executed_target in self.alive:
                    self.alive.remove(executed_target)

                exec_embed = discord.Embed(
                    title="⚖️ 処刑結果",
                    description=f"村人たちの投票により、**{executed_target.mention}** さんが処刑されました。\n\n彼の正体は… 【 **{self.roles[executed_target]}** 】 でした！",
                    color=discord.Color.red()
                )
                await self.channel.send(embed=exec_embed)

                alive_wolves = [p for p in self.alive if self.roles[p] == "🐺 人狼"]

                if not alive_wolves:
                    seer_text = f"今回の占い師は **{seer_player.mention}** でした！" if seer_player else ""
                    await self.channel.send(
                        f"🎉 **村人陣営の勝利！**\n"
                        f"人狼を見つけ出して処刑しました！\n"
                        f"今回の人狼は **{wolf_player.mention}** でした！\n"
                        f"{seer_text}"
                    )
                    break

                if len(self.alive) == 2 and len(alive_wolves) == 1:
                    seer_text = f"今回の占い師は **{seer_player.mention}** でした！" if seer_player else ""
                    await self.channel.send(
                        f"🐺 **人狼陣営の勝利！**\n"
                        f"生き残ったのは人狼（{alive_wolves[0].mention}）と村人1人のみになりました！\n"
                        f"今回の人狼は **{wolf_player.mention}** でした！\n"
                        f"{seer_text}"
                    )
                    break
            else:
                await self.channel.send("有効な投票がなかったため、本日の処刑は見送られました。")

        if self.channel.id in active_games:
            del active_games[self.channel.id]

@bot.tree.command(name="wolfgame", description="人狼ゲームの募集を開始します")
async def wolfgame_command(interaction: discord.Interaction):
    if interaction.channel.id in active_games:
        await interaction.response.send_message("このチャンネルではすでに人狼ゲームが進行中です！", ephemeral=True)
        return
    view = WolfLobbyView(host=interaction.user)
    await interaction.response.send_message(embed=view.get_embed(), view=view)

@bot.tree.command(name="wolfend", description="進行中の人狼ゲームを強制終了します")
async def wolfend_command(interaction: discord.Interaction):
    session = active_games.get(interaction.channel.id)
    if not session:
        await interaction.response.send_message("このチャンネルで進行中の人狼ゲームはありません。", ephemeral=True)
        return
    if interaction.user != session.host and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("ゲームを強制終了できるのはホストまたは管理者のみです！", ephemeral=True)
        return
    session.is_running = False
    if interaction.channel.id in active_games:
        del active_games[interaction.channel.id]
    await interaction.response.send_message("🛑 ホストによって人狼ゲームが強制終了されました。")
# ==========================================
# スラッシュコマンドの実装 (/create & /quiz)
# ==========================================
@bot.tree.command(name="create", description="新しいダイヤを作成し、各駅の時刻表を出力します。")
@app_commands.describe(
    路線="ダイヤを作成する路線名",
    種別="列車の種別（普通、快速、急行など）",
    始発駅="列車の出発駅",
    終着駅="列車の終点駅",
    始発時刻="出発時刻 (例: 12:00)"
)
async def create_dia_command(
    interaction: discord.Interaction,
    路線: str,
    種別: str,
    始発駅: str,
    終着駅: str,
    始発時刻: str
):
    if not await check_command_permission(interaction, "create"):
        return

    timetable, error = calculate_times(路線, 種別, 始発駅, 終着駅, 始発時刻)
    if error:
        await interaction.response.send_message(f"❌ エラー: {error}", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🚆 ダイヤ作成結果 ({種別})",
        description=f"**路線:** {路線}\n**運行区間:** {始発駅} ➡ {終着駅}",
        color=discord.Color.green()
    )

    schedule_text = ""
    for stop in timetable:
        schedule_text += f"**{stop['station']}駅** - 着 {stop['arr']} / 発 {stop['dep']}\n"

    embed.add_field(name="時刻表", value=schedule_text, inline=False)
    
    train_data = load_trains()
    train_id = f"TRN-{random.randint(1000, 9999)}"
    train_data[train_id] = {
        "route": 路線,
        "type": 種別,
        "timetable": timetable,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_trains(train_data)

    embed.set_footer(text=f"管理ID: {train_id} | 設定者: {interaction.user.name}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="quiz", description="尾羽急に関する鉄道クイズを出題します！正解するとダイヤ作成制限がリセットされます。")
async def quiz_command(interaction: discord.Interaction):
    quiz = random.choice(QUIZ_LIST)
    
    embed = discord.Embed(
        title="❓ 尾羽急 鉄道クイズ！",
        description=f"**問題:**\n{quiz['question']}\n\n下のボタンから正解を選択してください！",
        color=discord.Color.blue()
    )
    embed.set_footer(text="※制限時間 60秒 | 正解すると /create のクールダウンがリセットされます！")

    view = QuizView(quiz_data=quiz, user_id=interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view)

# minigame
# ボットの基本設定
# ==========================================
# 🎮 ミニゲームコーナー（おみくじ・じゃんけん・ロシアンルーレット・スロット）
# ==========================================

# 1. おみくじ (/omikuji)
@bot.tree.command(name="omikuji", description="今日の運勢を占います！")
async def omikuji(interaction: discord.Interaction):
    results = ["大吉 🌟", "中吉 ✨", "小吉 🌙", "吉 🌱", "凶 💧", "大凶 💀"]
    result = random.choice(results)
    
    embed = discord.Embed(
        title="⛩️ おみくじ",
        description=f"{interaction.user.mention} さんの今日の運勢は… **{result}** です！",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)

# 2. じゃんけん (/janken)
@bot.tree.command(name="janken", description="ボットとじゃんけん勝負をします！")
async def janken(interaction: discord.Interaction, 選択: str):
    choices = ["グー", "チョキ", "パー"]
    if 選択 not in choices:
        await interaction.response.send_message("「グー」「チョキ」「パー」の中から選んでね！", ephemeral=True)
        return
    
    bot_choice = random.choice(choices)
    
    if 選択 == bot_choice:
        outcome = "あいこです！ 🤝"
        color = discord.Color.light_gray()
    elif (
        (選択 == "グー" and bot_choice == "チョキ") or
        (選択 == "チョキ" and bot_choice == "パー") or
        (選択 == "パー" and bot_choice == "グー")
    ):
        outcome = "あなたの勝ちです！ 🎉"
        color = discord.Color.green()
    else:
        outcome = "あなたの負けです… 😢"
        color = discord.Color.red()
        
    embed = discord.Embed(
        title="✊ ✋ ✌️ じゃんけん勝負",
        description=f"あなた: **{選択}**\nボット: **{bot_choice}**\n\n**{outcome}**",
        color=color
    )
    await interaction.response.send_message(embed=embed)

# 3. ボタン式ロシアンルーレット (/russian)
class RussianButtonView(discord.ui.View):
    def __init__(self, user):
        super().__init__(timeout=60.0)
        self.user = user
        self.loser_index = random.randint(0, 3)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message("あなたが始めたゲームではありません！", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="ボタン①", style=discord.ButtonStyle.secondary, custom_id="btn_0")
    async def btn_0(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.check_result(interaction, 0)

    @discord.ui.button(label="ボタン②", style=discord.ButtonStyle.secondary, custom_id="btn_1")
    async def btn_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.check_result(interaction, 1)

    @discord.ui.button(label="ボタン③", style=discord.ButtonStyle.secondary, custom_id="btn_2")
    async def btn_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.check_result(interaction, 2)

    @discord.ui.button(label="ボタン④", style=discord.ButtonStyle.secondary, custom_id="btn_3")
    async def btn_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.check_result(interaction, 3)

    async def check_result(self, interaction, chosen_index):
        for child in self.children:
            child.disabled = True

        if chosen_index == self.loser_index:
            embed = discord.Embed(
                title="💥 ロシアンルーレット",
                description=f"{self.user.mention} さん、選んだボタンは… **ハズレ（ドカーン！）** 💥",
                color=discord.Color.red()
            )
        else:
            embed = discord.Embed(
                title="✨ ロシアンルーレット",
                description=f"{self.user.mention} さん、選んだボタンは… **セーフ！** 😌",
                color=discord.Color.green()
            )

        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

@bot.tree.command(name="russian", description="4つのボタンから1つ選ぶロシアンルーレット！")
async def russian(interaction: discord.Interaction):
    view = RussianButtonView(interaction.user)
    embed = discord.Embed(
        title="🔫 ロシアンルーレット",
        description=f"{interaction.user.mention} さん、4つのボタンのうち1つがハズレです。どれか一つを押してください！",
        color=discord.Color.dark_red()
    )
    await interaction.response.send_message(embed=embed, view=view)

import asyncio

# ==========================================
# 4. スロットゲーム (/slot) アニメーション付き
# ==========================================
@bot.tree.command(name="slot", description="スロットを回して運試しをしよう！")
async def slot(interaction: discord.Interaction):
    symbols = ["🍒", "🇯🇵", "🔔", "⭐", "💎", "📝"]
    
    # 最初に「スロットを回しています…」と送信する
    embed = discord.Embed(
        title="🎰 スロットマシン",
        description="【 🔄 | 🔄 | 🔄 】\n\n**スロット回転中……**",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed)
    
    # パタパタと絵文字を変える演出（3回繰り返す）
    for _ in range(3):
        await asyncio.sleep(0.6) # 0.6秒ごとに切り替え
        temp_result = [random.choice(symbols) for _ in range(3)]
        temp_embed = discord.Embed(
            title="🎰 スロットマシン",
            description=f"【 {temp_result[0]} | {temp_result[1]} | {temp_result[2]} 】\n\n**回転中…… 🔄**",
            color=discord.Color.blurple()
        )
        await interaction.edit_original_response(embed=temp_embed)
    
    # 最後に最終結果を決定
    await asyncio.sleep(0.6)
    result = [random.choice(symbols) for _ in range(3)]
    
    if result[0] == result[1] == result[2]:
        outcome = "大当たり！お見事です！ 🎉✨"
        color = discord.Color.gold()
    elif result[0] == result[1] or result[1] == result[2] or result[0] == result[2]:
        outcome = "惜しい！2つ揃いました！ 👍"
        color = discord.Color.green()
    else:
        outcome = "ハズレ…また挑戦してね！ 😢"
        color = discord.Color.red()
        
    final_embed = discord.Embed(
        title="🎰 スロットマシン",
        description=f"【 {result[0]} | {result[1]} | {result[2]} 】\n\n**{outcome}**",
        color=color
    )
    await interaction.edit_original_response(embed=final_embed)
# ==========================================
# 🔍 検索コーナー（AI検索・wiki検索）
# ==========================================

from openai import OpenAI
import os

# OpenAIクライアントの初期化
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# 5. ChatGPT検索 (/search)
@bot.tree.command(name="search", description="ChatGPTを使って質問や検索をします")
async def search(interaction: discord.Interaction, キーワード: str):
    await interaction.response.defer()
    
    try:
        # OpenAI APIを使った回答の生成
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",  # コスパが良く高速なモデル
            messages=[
                {"role": "system", "content": "あなたは優秀なアシスタントです。簡潔でわかりやすく答えてください。"},
                {"role": "user", "content": キーワード}
            ]
        )
        answer_text = response.choices[0].message.content
        
        if len(answer_text) > 1900:
            answer_text = answer_text[:1900] + "...\n（文字数オーバーのため省略しました）"

        embed = discord.Embed(
            title="🔍 AI検索結果 (ChatGPT)",
            description=f"**検索ワード:** {キーワード}\n\n{answer_text}",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        embed = discord.Embed(
            title="🔍 AI検索エラー",
            description=f"エラーが発生しました: `{e}`\n(OpenAI APIキーを確認してください)",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)
# ==========================================
# 🛠️ BOT管理部専用 !botinfo & !restart コマンド
# ==========================================
import psutil

ADMIN_ROLE_IDS = [1510021467167789104, 1528521149582151751]

error_logs = []
bot_start_time = time.time()
bot_last_online_time = time.time()

@bot.event
async def on_error(event, *args, **kwargs):
    import traceback
    error_msg = traceback.format_exc().splitlines()[-1]
    current_t = time.strftime("%H:%M:%S", time.localtime())
    error_logs.append(f"[{current_t}] {error_msg}")
    if len(error_logs) > 3:
        error_logs.pop(0)

@bot.command(name="botinfo")
async def botinfo_command(ctx):
    user_role_ids = [role.id for role in getattr(ctx.author, "roles", [])]
    has_permission = any(role_id in ADMIN_ROLE_IDS for role_id in user_role_ids)

    if not has_permission:
        return

    global bot_last_online_time
    bot_last_online_time = time.time()

    ping = round(bot.latency * 1000) if bot.latency is not None else 0
    
    process = psutil.Process(os.getpid())
    mem_bytes = process.memory_info().rss
    mem_mb = round(mem_bytes / (1024 * 1024), 1)
    mem_percent = round((mem_mb / 512) * 100, 1)

    uptime_seconds = int(time.time() - bot_start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    
    start_str = time.strftime("%Y/%m/%d %H:%M", time.localtime(bot_start_time))
    online_str = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(bot_last_online_time))

    if error_logs:
        error_content = "\n".join([f"{i+1}. `{log}`" for i, log in enumerate(error_logs)])
    else:
        error_content = "なし"

    embed = discord.Embed(title="🤖 Bot稼働状況", color=0x00ff00)
    embed.add_field(name="● 現在のステータス", value="正常稼働中", inline=False)
    embed.add_field(name="● Discord API接続状況", value="良好（Connected）", inline=False)
    embed.add_field(name="● 応答速度 (Ping)", value=f"{ping} ms", inline=False)
    embed.add_field(name="● メモリ使用率", value=f"{mem_percent}% ({mem_mb}MB / 512MB)", inline=False)
    embed.add_field(name="● 本日のエラー発生数", value=f"{len(error_logs)} 件 ⚠️", inline=False)
    embed.add_field(name="🚨 直近のエラー内容（最新3件まで）", value=error_content, inline=False)
    embed.add_field(name="● 起動してから", value=f"{hours}時間 {minutes}分 経過\n*({start_str} 起動)*", inline=False)
    embed.add_field(name="● Bot最終オンライン時刻", value=f"{online_str} (リアルタイム)", inline=False)

    await ctx.send(embed=embed)

@bot.command(name="restart")
async def restart_command(ctx):
    user_role_ids = [role.id for role in getattr(ctx.author, "roles", [])]
    has_permission = any(role_id in ADMIN_ROLE_IDS for role_id in user_role_ids)

    if not has_permission:
        return

    await ctx.send("🔄 ボットを再起動しています... (Render側で自動再起動されます)")
    print("⚠️ [管理者操作] !restart コマンドによりプロセスを終了します。")
    await bot.close()
    sys.exit(0)

# ==========================================
# 起動処理
# ==========================================
@bot.event
async def on_ready():
    print(f"ログインしました: {bot.user.name} (ID: {bot.user.id})")
    
    print("🔄 [起動処理] Discordから最新データベースの読み込みを開始します...")
    try:
        await sub.setup_sub_system(bot)
        print("✅ [起動処理] データベースの初期同期が完了しました。")
    except Exception as e:
        print(f"❌ [起動処理] データベース同期中にエラーが発生しました: {e}")

    print("⚙️ [起動処理] sub.py のスラッシュコマンドを登録中...")
    sub.setup_slash_commands(bot)

    print("🔄 [起動処理] quiz.py の設定データをDiscordから復旧中...")
    try:
        await load_config_from_discord(bot)
        print("✅ [起動処理] quiz.py のデータ復旧が完了しました。")
    except Exception as e:
        print(f"❌ [起動処理] quiz.py データ復旧中にエラーが発生しました: {e}")

    print("⚡ [起動処理] コマンドの重複をクリアして再同期中...")
    try:
        for guild in bot.guilds:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)

        synced = await bot.tree.sync()
        print(f"🚀 {len(synced)} 個のコマンドを同期しました。(重複解消済み)")
    except Exception as e:
        print(f"同期エラー: {e}")

if __name__ == "__main__":
    keep_alive()
    import os

# 環境変数からトークンとGeminiのAPIキーを読み込む
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


# (中略：各コマンドの定義部分)

# 起動処理
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("エラー: 環境変数 'DISCORD_TOKEN' が設定されていないか、見つかりません。")
