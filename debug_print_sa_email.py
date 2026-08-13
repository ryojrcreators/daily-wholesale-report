"""
使い捨てデバッグスクリプト。GitHub Secretsに登録されている GOOGLE_CREDENTIALS_JSON の
client_email だけを出力する（JSON本体や秘密鍵は一切出力しない）。

ローカルPCで新しく発行したサービスアカウント鍵と、実際にGitHub Actionsの自動化が
使っているサービスアカウントが同じかどうかを確認するために使う。
"""

import json
import os

data = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
print(f"client_email: {data['client_email']}")
