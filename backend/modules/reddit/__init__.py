"""Reddit Master module.

M1/M2/M3: в реестр регистрируется stub с COMING_SOON. В M4 stub
заменяется на полный RedditModule с роутерами, моделями, миграциями.

Переключение управляется фича-флагом FB_MASTER_REDDIT_MODULE_ENABLED.
"""

from backend.modules.reddit.module_stub import (
    RedditComingSoonModule,
    reddit_coming_soon_module,
)

__all__ = ["RedditComingSoonModule", "reddit_coming_soon_module"]
