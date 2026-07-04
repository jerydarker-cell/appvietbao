from __future__ import annotations

import getpass
import hashlib

pw = getpass.getpass("Nhập mật khẩu muốn hash: ")
print(hashlib.sha256(pw.encode("utf-8")).hexdigest())
print("Dán vào Secrets dạng: ADMIN_PASSWORD_SHA256 = \"...\"")
