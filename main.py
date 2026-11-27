import os
import threading
import time

import discord
import requests
from discord.ext import commands

import sub

# ==========================================
# Flask (Render / UptimeRobot 等の死活監視用)
# ==========================================
try:
    from flask import Flask
except ModuleNotFoundError:
    class Flask:
        def __init__(self, name):
            self.name = name

        def route(self, path):
            def decorator(func):
                return func
            return decorator

        def run(self, *args, **kwargs):
            pass
    print("警告: flask がインストールされていないため、ダミーを使用します。")

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is running"


@app.route("/health")
def health():
    return "OK", 200


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def self_ping():
    """10分ごとに自分を叩いてRenderのスリープを防ぐ"""
    url = "https://koushiki.onrender.com/health"
    while True:
        try:
            time.sleep(600)
            response = requests.get(url, timeout=30)
            print(f"Self-ping sent! Status: {response.status_code}")
        except Exception as e:
            print(f"Self-ping failed: {e}")


threading.Thread(target=run_flask, daemon=True).start()
threading.Thread(target=self_ping, daemon=True).start()


# ==========================================
# Discord Bot
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class MainBot(commands.Bot):
    async def setup_hook(self):
        # 起動時に1回だけ登録（on_readyは再接続で複数回呼ばれるためここで行う）
        sub.setup_points_commands(self)
        await self.add_cog(sub.Modmail(self))
        self.add_view(sub.SupportClaimView())  # 再起動後も「担当する」ボタンを有効に
        try:
            synced = await self.tree.sync()
            print(f"🚀 {len(synced)} 個のコマンドを同期しました。")
        except Exception as e:
            print(f"同期エラー: {e}")


bot = MainBot(command_prefix="!", intents=intents)
_points_synced = False


@bot.event
async def on_ready():
    global _points_synced
    print(f"ログインしました: {bot.user.name} (ID: {bot.user.id})")
    if not _points_synced:
        _points_synced = True
        try:
            await sub.sync_points_from_discord(bot)
        except Exception as e:
            print(f"❌ ポイントデータの同期中にエラーが発生しました: {e}")


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")


def run_bot_forever():
    """Discord API の一時的な通信エラーでプロセスが落ちないよう、再試行する"""
    retry_wait = 10
    max_retry_wait = 300
    while True:
        try:
            bot.run(DISCORD_TOKEN)
            break
        except Exception as e:
            import traceback
            print(f"⚠️ [起動処理] bot.run() が例外で終了しました: {e}")
            traceback.print_exc()
            print(f"🔁 {retry_wait}秒後に再接続を試みます...")
            time.sleep(retry_wait)
            retry_wait = min(retry_wait * 2, max_retry_wait)


if __name__ == "__main__":
    if DISCORD_TOKEN:
        run_bot_forever()
    else:
        print("エラー: 環境変数 'DISCORD_TOKEN' が設定されていないか、見つかりません。")
