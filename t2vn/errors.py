"""專案共用的例外類型。所有可預期的錯誤都繼承 T2vnError，CLI 可統一攔截。"""
from __future__ import annotations


class T2vnError(Exception):
    """所有可預期錯誤的基底類別。"""


class ConfigError(T2vnError):
    """config.yaml 缺漏、格式錯誤或路徑設定不合規則。"""


class WriteProtectedError(ConfigError):
    """嘗試寫入唯讀目錄（書目錄、novel-studio），或寫到專案目錄以外。"""


class SourceError(T2vnError):
    """讀取書資料時發現格式問題（未映射的欄位、表格欄數不對等）。"""


class SchemaCheckError(T2vnError):
    """schema 檔案缺漏或無法載入。"""


class JobStopped(T2vnError):
    """使用者在網頁按了「停止」，任務在安全的地方中斷（不是錯誤）。"""

