#!/usr/bin/env python3
"""
YouTube OAuth 2.0 认证模块

首次运行会弹出浏览器完成授权，token 存入 youtube_token.json，之后自动刷新。

依赖：
    pip install google-auth-oauthlib google-api-python-client
"""

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

PROJECT_ROOT = Path(__file__).parent.parent.parent
CLIENT_SECRET = PROJECT_ROOT / "client_secret_187061917532-lbrhj238dlr6lm872deh3gnkaoetoio0.apps.googleusercontent.com.json"
TOKEN_FILE = Path(__file__).parent / "youtube_token.json"


def get_youtube_client():
    """返回已认证的 YouTube API client。首次运行需要浏览器授权。"""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Token 已过期，自动刷新...")
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                raise FileNotFoundError(f"找不到 client_secret.json：{CLIENT_SECRET}")
            print("首次授权，请在弹出的浏览器页面完成 Google 账号登录...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())
        print(f"Token 已保存到 {TOKEN_FILE}")

    return build("youtube", "v3", credentials=creds)


if __name__ == "__main__":
    print("正在验证 YouTube API 授权...")
    youtube = get_youtube_client()
    resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        print("✗ 授权成功但找不到频道，确认账号是否正确")
    else:
        print(f"✓ 授权成功！频道：{items[0]['snippet']['title']}")
