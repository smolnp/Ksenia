# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Dialogs facade — реэкспорт для обратной совместимости."""
from dialogs_base import (  # noqa: F401
    BaseDialog, IconProvider, _NumericItem,
    _is_qobject_valid, _is_gui_thread,
    make_table, make_action, fill_channels_table,
    json_import_dialog, json_export_dialog,
    hint_label,
)
from dialogs_help import (  # noqa: F401
    HelpBrowser, HelpDialog, SupportDialog,
)
from dialogs_edit import (  # noqa: F401
    PlaylistHeaderDialog, RemoveMetadataDialog, MassEditDialog,
    DuplicateFinderDialog, ComparePlaylistsDialog,
    GroupTemplateListDialog,
)
from dialogs_blacklist import (  # noqa: F401
    BlockDomainDialog, DomainBlacklistDialog, BlacklistDialog,
    DomainUserAgentEditDialog, DomainUserAgentDialog,
)
from dialogs_sources import (  # noqa: F401
    LinkSourceEditDialog, SourcesFinderDialog,
    LinkSourceManagerDialog, LinkSelectionDialog,
    PlaylistCreationOptions, PlaylistFromSourcesDialog,
)
from dialogs_settings import (  # noqa: F401
    LinkReplacementSettingsDialog, GeneralSettingsDialog,
    CacheManagerDialog,
)