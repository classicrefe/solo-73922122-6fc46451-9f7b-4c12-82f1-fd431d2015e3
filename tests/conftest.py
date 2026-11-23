import os
import tempfile

# 必须在导入 app 之前设置数据库位置（每个测试会话一个独立临时库）
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='street_archive_')}/test.db"
os.environ["VERIFY_DELAY_SECONDS"] = "0"
