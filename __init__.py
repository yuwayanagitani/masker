from __future__ import annotations

import base64
import json
import math
import os
import re
import time
import urllib.request
import uuid
import copy
from typing import Any, Optional, cast

from aqt import gui_hooks, mw
from aqt.qt import (
    QAction,
    QBuffer,
    QByteArray,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QImage,
    QIODevice,
    QLabel,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QComboBox,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QUrl,
    QVBoxLayout,
    QWidget,
    Qt,
    QTimer,
    QGuiApplication,
)
from aqt.utils import askUser, showInfo, showWarning, tooltip
from aqt.webview import AnkiWebView, AnkiWebViewKind


ADDON_PACKAGE = __name__
ADDON_DIR = os.path.dirname(__file__)

MODEL_NAME_DEFAULT = "Image Masker"
FIELD_IMAGEFILE = "ImageFile"
FIELD_IMAGEHTML = "ImageHTML"
FIELD_MASKSB64 = "MasksB64"
FIELD_ACTIVEIDX = "ActiveIndex"
FIELD_GROUPID = "GroupId"
FIELD_EXTRA = "Extra"
FIELD_SORTKEY = "SortKey"
FIELD_TITLE = "Title"
FIELD_EXPLANATION = "Explanation"
FIELD_MASKLABEL = "MaskLabel"
FIELD_NO = "No"
FIELD_INTERNAL = "InternalData"


# -------------------- config --------------------

# ✅ NESTED ONLY (legacy/flat keys are NOT supported)
DEFAULT_CFG: dict[str, Any] = {
    "01_general": {
        "enabled": True,
        "note_type_name": MODEL_NAME_DEFAULT,
        # ✅ moved here (instead of 05_advanced.*)
        "always_update_note_type_templates": False,
        # not implemented (kept as harmless option)
        "auto_open_browser_after_create": False,
    },
    "02_editor": {
        "add_editor_button": True,
        "editor_button_label": "🖼️",
        "editor_button_tooltip": "Create / edit image occlusion notes",
    },
    "03_masks": {
        "default_fill_front": "rgba(245,179,39,1)",
        "default_fill_other": "rgba(255,215,0,0.35)",
        "default_stroke": "rgba(0,0,0,0.65)",
        "outline_width_px": 2,
    },
    "04_ai": {
        "enable_ai": False,
        "enable_metadata_ai": False,
        "provider": "openai",  # "openai" | "gemini"
        "max_suggestions": 24,
        "openai": {
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.openai.com/v1/responses",
            "model": "gpt-4.1-mini",
            "timeout_sec": 45,
            "max_output_tokens": 800,
        },
        "gemini": {
            "api_key_env": "GEMINI_API_KEY",
            "endpoint": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            "model": "gemini-2.5-flash",
            "timeout_sec": 45,
            "max_output_tokens": 800,
        },
    },
    "05_image_processing": {
        # Image shown in the editor (WebView)
        "display": {
            "max_side_px": 1600,
            "jpeg_quality": 85,
        },
        # Image sent to AI for mask suggestions
        "ai_suggest": {
            "max_side_px": 1024,
            "jpeg_quality": 80,
        },
        # Image sent to AI for title/explanation generation
        "ai_metadata": {
            "max_side_px": 1024,
            "jpeg_quality": 80,
        },
    },
}

_CFG_CACHE: dict[str, Any] | None = None

def _invalidate_config_cache(*args, **kwargs) -> None:
    """Called when config is changed in Anki's add-on config UI."""
    global _CFG_CACHE
    _CFG_CACHE = None


def _invalidate_config_cache_keep_json(json_text: str, *args, **kwargs) -> str:
    """For *_will_save_json hooks: must return JSON string."""
    _invalidate_config_cache()
    return json_text



def _cfg() -> dict[str, Any]:
    try:
        return mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    except Exception:
        return {}


def _cfg_merged() -> dict[str, Any]:
    """Return merged config (user config + defaults) without mutating user config."""
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE

    user = _cfg()
    if not isinstance(user, dict):
        user = {}

    # IMPORTANT: copy so we don't mutate the object returned by getConfig()
    merged = copy.deepcopy(user)
    if not isinstance(merged, dict):
        merged = {}

    _deep_merge_defaults(merged, DEFAULT_CFG)
    _CFG_CACHE = merged
    return merged



def _deep_merge_defaults(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Recursively set missing keys from src into dst (non-destructive)."""
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
            continue
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge_defaults(cast(dict[str, Any], dst[k]), cast(dict[str, Any], v))


def _cfg_get(path: list[str], default: Any) -> Any:
    cur: Any = _cfg_merged()
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _cfg_set(conf: dict[str, Any], path: list[str], value: Any) -> None:
    cur: Any = conf
    for p in path[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[path[-1]] = value


def _write_config(conf: dict[str, Any]) -> None:
    # Keep unknown keys: start from existing user config, then overwrite known paths.
    try:
        mw.addonManager.writeConfig(ADDON_PACKAGE, conf)
        return
    except Exception:
        pass
    # Fallback for older Anki builds (rare)
    try:
        mw.addonManager.setConfig(ADDON_PACKAGE, conf)
    except Exception:
        pass


# -------------------- custom config GUI --------------------

class ConfigDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Image Masker — Settings")
        self.setMinimumWidth(760)

        self._conf = copy.deepcopy(_cfg_merged())

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # ---- General ----
        w_gen = QWidget()
        f_gen = QFormLayout(w_gen)
        f_gen.setVerticalSpacing(10)

        self.enabled = QCheckBox("Enable Image Masker")
        self.enabled.setChecked(bool(_cfg_get(["01_general", "enabled"], True)))
        f_gen.addRow(self.enabled)

        self.note_type = QLineEdit(str(_cfg_get(["01_general", "note_type_name"], MODEL_NAME_DEFAULT) or MODEL_NAME_DEFAULT))
        f_gen.addRow("Note type name", self.note_type)

        self.always_update = QCheckBox("Always update note type templates/CSS on startup")
        self.always_update.setChecked(bool(_cfg_get(["01_general", "always_update_note_type_templates"], False)))
        f_gen.addRow(self.always_update)

        self.auto_open_browser = QCheckBox("(Reserved) Auto-open browser after create")
        self.auto_open_browser.setChecked(bool(_cfg_get(["01_general", "auto_open_browser_after_create"], False)))
        f_gen.addRow(self.auto_open_browser)

        self.tabs.addTab(self._wrap_scroll(w_gen), "General")

        # ---- Editor ----
        w_ed = QWidget()
        f_ed = QFormLayout(w_ed)
        f_ed.setVerticalSpacing(10)

        self.add_editor_button = QCheckBox("Add button to the Add Cards editor")
        self.add_editor_button.setChecked(bool(_cfg_get(["02_editor", "add_editor_button"], True)))
        f_ed.addRow(self.add_editor_button)

        self.editor_label = QLineEdit(str(_cfg_get(["02_editor", "editor_button_label"], "🖼️") or "🖼️"))
        f_ed.addRow("Editor button label", self.editor_label)

        self.editor_tip = QLineEdit(str(_cfg_get(["02_editor", "editor_button_tooltip"], "Create / edit image occlusion notes") or "Create / edit image occlusion notes"))
        f_ed.addRow("Editor button tooltip", self.editor_tip)

        self.tabs.addTab(self._wrap_scroll(w_ed), "Editor")

        # ---- Masks ----
        w_m = QWidget()
        f_m = QFormLayout(w_m)
        f_m.setVerticalSpacing(10)

        self.fill_front = QLineEdit(str(_cfg_get(["03_masks", "default_fill_front"], "rgba(245,179,39,1)") or "rgba(245,179,39,1)"))
        f_m.addRow("Front fill (active)", self.fill_front)

        self.fill_other = QLineEdit(str(_cfg_get(["03_masks", "default_fill_other"], "rgba(255,215,0,0.35)") or "rgba(255,215,0,0.35)"))
        f_m.addRow("Fill (others)", self.fill_other)

        self.stroke = QLineEdit(str(_cfg_get(["03_masks", "default_stroke"], "rgba(0,0,0,0.65)") or "rgba(0,0,0,0.65)"))
        f_m.addRow("Outline stroke", self.stroke)

        self.outline_px = QSpinBox()
        self.outline_px.setRange(1, 12)
        self.outline_px.setValue(int(_cfg_get(["03_masks", "outline_width_px"], 2)))
        f_m.addRow("Outline width (px)", self.outline_px)

        self.tabs.addTab(self._wrap_scroll(w_m), "Masks")

        # ---- AI ----
        w_ai = QWidget()
        f_ai = QFormLayout(w_ai)
        f_ai.setVerticalSpacing(10)

        self.enable_ai = QCheckBox("Enable AI mask suggestions")
        self.enable_ai.setChecked(bool(_cfg_get(["04_ai", "enable_ai"], False)))
        f_ai.addRow(self.enable_ai)

        self.enable_meta_ai = QCheckBox("Enable AI title/explanation generation")
        self.enable_meta_ai.setChecked(bool(_cfg_get(["04_ai", "enable_metadata_ai"], False)))
        f_ai.addRow(self.enable_meta_ai)

        self.provider = QComboBox()
        self.provider.addItems(["openai", "gemini"])
        self.provider.setCurrentText(str(_cfg_get(["04_ai", "provider"], "openai") or "openai").lower())
        f_ai.addRow("Provider", self.provider)

        self.max_sug = QSpinBox()
        self.max_sug.setRange(1, 200)
        self.max_sug.setValue(int(_cfg_get(["04_ai", "max_suggestions"], 24)))
        f_ai.addRow("Max suggestions", self.max_sug)

        # OpenAI
        box_o = QGroupBox("OpenAI")
        fo = QFormLayout(box_o)
        fo.setVerticalSpacing(8)
        self.oa_env = QLineEdit(str(_cfg_get(["04_ai", "openai", "api_key_env"], "OPENAI_API_KEY")))
        fo.addRow("API key env", self.oa_env)
        self.oa_url = QLineEdit(str(_cfg_get(["04_ai", "openai", "base_url"], "https://api.openai.com/v1/responses")))
        fo.addRow("Base URL", self.oa_url)
        self.oa_model = QLineEdit(str(_cfg_get(["04_ai", "openai", "model"], "gpt-4.1-mini")))
        fo.addRow("Model", self.oa_model)
        self.oa_timeout = QSpinBox()
        self.oa_timeout.setRange(5, 300)
        self.oa_timeout.setValue(int(_cfg_get(["04_ai", "openai", "timeout_sec"], 45)))
        fo.addRow("Timeout (sec)", self.oa_timeout)
        self.oa_maxout = QSpinBox()
        self.oa_maxout.setRange(16, 5000)
        self.oa_maxout.setValue(int(_cfg_get(["04_ai", "openai", "max_output_tokens"], 800)))
        fo.addRow("Max output tokens", self.oa_maxout)
        f_ai.addRow(box_o)

        # Gemini
        box_g = QGroupBox("Gemini")
        fg = QFormLayout(box_g)
        fg.setVerticalSpacing(8)
        self.ge_env = QLineEdit(str(_cfg_get(["04_ai", "gemini", "api_key_env"], "GEMINI_API_KEY")))
        fg.addRow("API key env", self.ge_env)
        self.ge_ep = QLineEdit(str(_cfg_get(["04_ai", "gemini", "endpoint"], "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent")))
        fg.addRow("Endpoint", self.ge_ep)
        self.ge_model = QLineEdit(str(_cfg_get(["04_ai", "gemini", "model"], "gemini-2.5-flash")))
        fg.addRow("Model", self.ge_model)
        self.ge_timeout = QSpinBox()
        self.ge_timeout.setRange(5, 300)
        self.ge_timeout.setValue(int(_cfg_get(["04_ai", "gemini", "timeout_sec"], 45)))
        fg.addRow("Timeout (sec)", self.ge_timeout)
        self.ge_maxout = QSpinBox()
        self.ge_maxout.setRange(16, 5000)
        self.ge_maxout.setValue(int(_cfg_get(["04_ai", "gemini", "max_output_tokens"], 800)))
        fg.addRow("Max output tokens", self.ge_maxout)
        f_ai.addRow(box_g)

        self.tabs.addTab(self._wrap_scroll(w_ai), "AI")

        # ---- Image Processing ----
        w_ip = QWidget()
        f_ip = QFormLayout(w_ip)
        f_ip.setVerticalSpacing(10)

        def _mk_pair(title: str, section: str, default_side: int, default_q: int):
            box = QGroupBox(title)
            ff = QFormLayout(box)
            ff.setVerticalSpacing(8)
            sp = QSpinBox()
            sp.setRange(256, 4096)
            sp.setValue(int(_cfg_get(["05_image_processing", section, "max_side_px"], default_side)))
            qq = QSpinBox()
            qq.setRange(30, 95)
            qq.setValue(int(_cfg_get(["05_image_processing", section, "jpeg_quality"], default_q)))
            ff.addRow("Max side (px)", sp)
            ff.addRow("JPEG quality", qq)
            return box, sp, qq

        box_d, self.disp_side, self.disp_q = _mk_pair("Display in editor (WebView)", "display", 1600, 85)
        box_s, self.sug_side, self.sug_q = _mk_pair("AI suggest image", "ai_suggest", 1024, 80)
        box_m, self.meta_side, self.meta_q = _mk_pair("AI metadata image", "ai_metadata", 1024, 80)
        f_ip.addRow(box_d)
        f_ip.addRow(box_s)
        f_ip.addRow(box_m)

        self.tabs.addTab(self._wrap_scroll(w_ip), "Image")

        # ---- buttons ----
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.btn_reset = QPushButton("Reset to defaults")
        btns.addButton(self.btn_reset, QDialogButtonBox.ButtonRole.ResetRole)
        self.btn_reset.clicked.connect(self._reset_defaults)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _wrap_scroll(self, inner: QWidget) -> QScrollArea:
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QScrollArea.Shape.NoFrame)
        sc.setWidget(inner)
        return sc

    def _reset_defaults(self) -> None:
        d = DEFAULT_CFG
        self.enabled.setChecked(bool(d["01_general"]["enabled"]))
        self.note_type.setText(str(d["01_general"]["note_type_name"]))
        self.always_update.setChecked(bool(d["01_general"]["always_update_note_type_templates"]))
        self.auto_open_browser.setChecked(bool(d["01_general"]["auto_open_browser_after_create"]))

        self.add_editor_button.setChecked(bool(d["02_editor"]["add_editor_button"]))
        self.editor_label.setText(str(d["02_editor"]["editor_button_label"]))
        self.editor_tip.setText(str(d["02_editor"]["editor_button_tooltip"]))

        self.fill_front.setText(str(d["03_masks"]["default_fill_front"]))
        self.fill_other.setText(str(d["03_masks"]["default_fill_other"]))
        self.stroke.setText(str(d["03_masks"]["default_stroke"]))
        self.outline_px.setValue(int(d["03_masks"]["outline_width_px"]))

        self.enable_ai.setChecked(bool(d["04_ai"]["enable_ai"]))
        self.enable_meta_ai.setChecked(bool(d["04_ai"]["enable_metadata_ai"]))
        self.provider.setCurrentText(str(d["04_ai"]["provider"]))
        self.max_sug.setValue(int(d["04_ai"]["max_suggestions"]))

        self.oa_env.setText(str(d["04_ai"]["openai"]["api_key_env"]))
        self.oa_url.setText(str(d["04_ai"]["openai"]["base_url"]))
        self.oa_model.setText(str(d["04_ai"]["openai"]["model"]))
        self.oa_timeout.setValue(int(d["04_ai"]["openai"]["timeout_sec"]))
        self.oa_maxout.setValue(int(d["04_ai"]["openai"]["max_output_tokens"]))

        self.ge_env.setText(str(d["04_ai"]["gemini"]["api_key_env"]))
        self.ge_ep.setText(str(d["04_ai"]["gemini"]["endpoint"]))
        self.ge_model.setText(str(d["04_ai"]["gemini"]["model"]))
        self.ge_timeout.setValue(int(d["04_ai"]["gemini"]["timeout_sec"]))
        self.ge_maxout.setValue(int(d["04_ai"]["gemini"]["max_output_tokens"]))

        self.disp_side.setValue(int(d["05_image_processing"]["display"]["max_side_px"]))
        self.disp_q.setValue(int(d["05_image_processing"]["display"]["jpeg_quality"]))
        self.sug_side.setValue(int(d["05_image_processing"]["ai_suggest"]["max_side_px"]))
        self.sug_q.setValue(int(d["05_image_processing"]["ai_suggest"]["jpeg_quality"]))
        self.meta_side.setValue(int(d["05_image_processing"]["ai_metadata"]["max_side_px"]))
        self.meta_q.setValue(int(d["05_image_processing"]["ai_metadata"]["jpeg_quality"]))

    def _on_ok(self) -> None:
        user = _cfg()
        if not isinstance(user, dict):
            user = {}
        conf = copy.deepcopy(user)

        _cfg_set(conf, ["01_general", "enabled"], bool(self.enabled.isChecked()))
        _cfg_set(conf, ["01_general", "note_type_name"], str(self.note_type.text()).strip() or MODEL_NAME_DEFAULT)
        _cfg_set(conf, ["01_general", "always_update_note_type_templates"], bool(self.always_update.isChecked()))
        _cfg_set(conf, ["01_general", "auto_open_browser_after_create"], bool(self.auto_open_browser.isChecked()))

        _cfg_set(conf, ["02_editor", "add_editor_button"], bool(self.add_editor_button.isChecked()))
        _cfg_set(conf, ["02_editor", "editor_button_label"], str(self.editor_label.text()))
        _cfg_set(conf, ["02_editor", "editor_button_tooltip"], str(self.editor_tip.text()))

        _cfg_set(conf, ["03_masks", "default_fill_front"], str(self.fill_front.text()))
        _cfg_set(conf, ["03_masks", "default_fill_other"], str(self.fill_other.text()))
        _cfg_set(conf, ["03_masks", "default_stroke"], str(self.stroke.text()))
        _cfg_set(conf, ["03_masks", "outline_width_px"], int(self.outline_px.value()))

        _cfg_set(conf, ["04_ai", "enable_ai"], bool(self.enable_ai.isChecked()))
        _cfg_set(conf, ["04_ai", "enable_metadata_ai"], bool(self.enable_meta_ai.isChecked()))
        _cfg_set(conf, ["04_ai", "provider"], str(self.provider.currentText()).lower())
        _cfg_set(conf, ["04_ai", "max_suggestions"], int(self.max_sug.value()))

        _cfg_set(conf, ["04_ai", "openai", "api_key_env"], str(self.oa_env.text()).strip() or "OPENAI_API_KEY")
        _cfg_set(conf, ["04_ai", "openai", "base_url"], str(self.oa_url.text()).strip() or "https://api.openai.com/v1/responses")
        _cfg_set(conf, ["04_ai", "openai", "model"], str(self.oa_model.text()).strip() or "gpt-4.1-mini")
        _cfg_set(conf, ["04_ai", "openai", "timeout_sec"], int(self.oa_timeout.value()))
        _cfg_set(conf, ["04_ai", "openai", "max_output_tokens"], int(self.oa_maxout.value()))

        _cfg_set(conf, ["04_ai", "gemini", "api_key_env"], str(self.ge_env.text()).strip() or "GEMINI_API_KEY")
        _cfg_set(conf, ["04_ai", "gemini", "endpoint"], str(self.ge_ep.text()).strip() or "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent")
        _cfg_set(conf, ["04_ai", "gemini", "model"], str(self.ge_model.text()).strip() or "gemini-2.5-flash")
        _cfg_set(conf, ["04_ai", "gemini", "timeout_sec"], int(self.ge_timeout.value()))
        _cfg_set(conf, ["04_ai", "gemini", "max_output_tokens"], int(self.ge_maxout.value()))

        _cfg_set(conf, ["05_image_processing", "display", "max_side_px"], int(self.disp_side.value()))
        _cfg_set(conf, ["05_image_processing", "display", "jpeg_quality"], int(self.disp_q.value()))
        _cfg_set(conf, ["05_image_processing", "ai_suggest", "max_side_px"], int(self.sug_side.value()))
        _cfg_set(conf, ["05_image_processing", "ai_suggest", "jpeg_quality"], int(self.sug_q.value()))
        _cfg_set(conf, ["05_image_processing", "ai_metadata", "max_side_px"], int(self.meta_side.value()))
        _cfg_set(conf, ["05_image_processing", "ai_metadata", "jpeg_quality"], int(self.meta_q.value()))

        _write_config(conf)
        _invalidate_config_cache()

        try:
            ensure_note_type()
        except Exception:
            pass

        self.accept()


def _open_settings_dialog() -> None:
    dlg = ConfigDialog(mw)
    dlg.exec()


def _install_config_action() -> None:
    try:
        mw.addonManager.setConfigAction(ADDON_PACKAGE, _open_settings_dialog)
    except Exception:
        # If not supported, Anki will fall back to JSON config UI.
        pass



# -------------------- web assets --------------------

def _read_web(rel: str) -> str:
    path = os.path.join(ADDON_DIR, "web", rel)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _build_editor_html_inline() -> str:
    html = _read_web("editor.html")
    css = _read_web("editor.css")
    js = _read_web("editor.js")
    html = html.replace("<!-- AIOE_INLINE_CSS -->", f"<style>\n{css}\n</style>")
    html = html.replace("<!-- AIOE_INLINE_JS -->", f"<script>\n{js}\n</script>")
    return html


# -------------------- note type --------------------

def ensure_note_type() -> None:
    if not mw.col:
        return

    model_name = _cfg_get(["01_general", "note_type_name"], MODEL_NAME_DEFAULT) or MODEL_NAME_DEFAULT
    always_update = bool(_cfg_get(["01_general", "always_update_note_type_templates"], False))

    mm = mw.col.models
    m = mm.by_name(model_name)

    runtime_js = _read_web("runtime.js")
    runtime_css = _read_web("runtime.css")

    css = runtime_css + "\n" + r"""
    .aioe-center { display: flex; justify-content: center; }
    .aioe-center #aioe-root { width: 100%; max-width: 100%; display: flex; justify-content: center; }
    .aioe-center .aioe-img { margin: 0 auto; }

    .aioe-title { font-size: 16px; font-weight: 600; margin: 8px 0 12px; text-align: center; }

    .aioe-explanation {
      margin: 12px auto 0;
      max-width: 720px;
      padding: 10px 14px;
      border: 1px solid #333;
      border-radius: 12px;
      background: rgba(255,255,255,0.04);
      font-size: 13px;
      line-height: 1.45;
      white-space: pre-wrap;
      text-align: left;
    }

    .aioe-explanation:empty {
    display: none;
    }
    """

    def _data_attrs(side: str) -> str:
        # Phase 3: rendering reads InternalData only (no legacy data-active / data-masks-b64)
        return (
            f'data-side="{side}" '
            f'data-fill-front="{_cfg_get(["03_masks","default_fill_front"], "rgba(245,179,39,1)")}" '
            f'data-fill-other="{_cfg_get(["03_masks","default_fill_other"], "rgba(255,215,0,0.35)")}" '
            f'data-stroke="{_cfg_get(["03_masks","default_stroke"], "rgba(0,0,0,0.65)")}" '
            f'data-outline-px="{_cfg_get(["03_masks","outline_width_px"], 2)}"'
        )

    def _fld(name: str) -> str:
        return "{{{{{}}}}}".format(name)

    front = f"""
    <div class="aioe-title">{_fld(FIELD_TITLE)}</div>
    <div class="aioe-center">
      <div id="aioe-root" {_data_attrs("front")}>
      <script class="aioe-internal" type="application/json">{_fld(FIELD_INTERNAL)}</script>
        {_fld(FIELD_IMAGEHTML)}
      </div>
    </div>
    <script>{runtime_js}</script>
    """

    back = f"""
    <div class="aioe-title">{_fld(FIELD_TITLE)}</div>
    <div class="aioe-center">
      <div id="aioe-root" {_data_attrs("back")}>
      <script class="aioe-internal" type="application/json">{_fld(FIELD_INTERNAL)}</script>
        {_fld(FIELD_IMAGEHTML)}
      </div>
    </div>
    <div class="aioe-explanation">{_fld(FIELD_EXPLANATION)}</div>
    <script>{runtime_js}</script>
    """

    def _set_field_collapsed(model: dict, field_name: str, collapsed: bool) -> None:
        flds = model.get("flds") or []
        for f in flds:
            if isinstance(f, dict) and f.get("name") == field_name:
                # Anki builds differ; try the common keys.
                f["collapsed"] = bool(collapsed)
                f["collapsedByDefault"] = bool(collapsed)
                f["collapsed_default"] = bool(collapsed)
                return

    def _apply_field_ui_defaults(model: dict) -> None:
        # 例：見た目に直接関係しないものを折りたたむ（好みで調整OK）
        for fn in [FIELD_IMAGEFILE, FIELD_MASKSB64, FIELD_ACTIVEIDX, FIELD_GROUPID, FIELD_MASKLABEL, FIELD_INTERNAL]:
            _set_field_collapsed(model, fn, True)

        # 表示上触りやすいものは開いたまま（必要なら True にしてOK）
        _set_field_collapsed(model, FIELD_TITLE, True)
        _set_field_collapsed(model, FIELD_NO, True)
        _set_field_collapsed(model, FIELD_EXPLANATION, False)
        _set_field_collapsed(model, FIELD_IMAGEHTML, False)


    def _field_names(model: dict) -> list[str]:
        flds = model.get("flds") or []
        names: list[str] = []
        for f in flds:
            if isinstance(f, dict):
                name = f.get("name")
                if isinstance(name, str):
                    names.append(name)
        return names

    need_fields = [
        FIELD_SORTKEY,
        FIELD_IMAGEHTML,
        FIELD_EXPLANATION,
        FIELD_TITLE,
        FIELD_NO,
        FIELD_IMAGEFILE,
        FIELD_GROUPID,
        FIELD_MASKLABEL,  
        FIELD_INTERNAL,  
    ]

    if not m:
        m = mm.new(model_name)
        for fn in need_fields:
            mm.add_field(m, mm.new_field(fn))

        t = mm.new_template("Card 1")
        t["qfmt"] = front
        t["afmt"] = back
        m["tmpls"] = [t]
        m["css"] = css
        _apply_field_ui_defaults(m)
        mm.add(m)
        return


    # Even when `always_update` is False, we still ensure required fields/CSS exist.
    # We only rewrite templates when:
    # - always_update is True, or
    # - templates still contain <img src={{ImageFile}}> (Media Check warning case).
    existing = set(_field_names(m))
    for fn in need_fields:
        if fn not in existing:
            mm.add_field(m, mm.new_field(fn))

    m["css"] = css


    need_update_templates = bool(always_update)
    try:
        tmpls = m.get("tmpls") or []
        if tmpls:
            q = str(tmpls[0].get("qfmt") or "")
            a = str(tmpls[0].get("afmt") or "")
            if (FIELD_IMAGEFILE in q) or (FIELD_IMAGEFILE in a):
                need_update_templates = True
    except Exception:
        pass

    if not need_update_templates:
        try:
            names = _field_names(m)
            if FIELD_SORTKEY in names:
                m["sortf"] = names.index(FIELD_SORTKEY)
        except Exception:
            pass
        _apply_field_ui_defaults(m)
        mm.save(m)

        # still migrate existing notes once (best-effort)
        _migrate_image_html_fields(model_name)
        return

    if not m.get("tmpls"):
        t = mm.new_template("Card 1")
        t["qfmt"] = front
        t["afmt"] = back
        m["tmpls"] = [t]
    else:
        m["tmpls"][0]["qfmt"] = front
        m["tmpls"][0]["afmt"] = back

    try:
        names = _field_names(m)
        if FIELD_SORTKEY in names:
            m["sortf"] = names.index(FIELD_SORTKEY)
    except Exception:
        pass

    _apply_field_ui_defaults(m)
    mm.save(m)

    # --- migrate existing notes so Media Check can detect files ---
    _migrate_image_html_fields(model_name)





# -------------------- migration --------------------

_MIGRATED_IMAGE_HTML = False

def _migrate_image_html_fields(model_name: str, limit: int = 500) -> None:
    """Populate ImageHTML from ImageFile for existing notes.

    This avoids using <img src={{Field}}> in templates, so Anki's Media Check can detect used files.
    Runs once per Anki session (best-effort) and is capped by `limit` for safety.
    """
    global _MIGRATED_IMAGE_HTML
    if _MIGRATED_IMAGE_HTML:
        return
    _MIGRATED_IMAGE_HTML = True

    if not mw.col:
        return
    try:
        nids = mw.col.find_notes(f'note:"{model_name}"')
    except Exception:
        return

    changed = 0
    for nid in nids:
        if changed >= limit:
            break
        try:
            note = mw.col.get_note(nid)
            imgfile = (note[FIELD_IMAGEFILE] or "").strip()
            if not imgfile:
                continue
            if (note.get(FIELD_IMAGEHTML, "") if hasattr(note, "get") else note[FIELD_IMAGEHTML]).strip():
                continue
            note[FIELD_IMAGEHTML] = f'<img class="aioe-img" src="{imgfile}">'
            note.flush()
            changed += 1
        except Exception:
            continue

    if changed:
        try:
            mw.col.save()
        except Exception:
            pass



# -------------------- helpers --------------------

def _guess_mime(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    if lower.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


def _media_abs_path(filename: str) -> str:
    return os.path.join(mw.col.media.dir(), filename)


def _import_image_from_clipboard() -> str | None:
    cb = QGuiApplication.clipboard()
    img: QImage | None = cb.image()

    if img is None or img.isNull():
        return None
    if not mw.col:
        return None

    ts = int(time.time() * 1000)
    filename = f"clipboard_{ts}.png"
    abs_path = os.path.join(mw.col.media.dir(), filename)

    if not img.save(abs_path, "PNG"):
        return None

    try:
        mw.col.media.addFile(abs_path)
    except Exception:
        pass

    return filename


def _clipboard_image_signature(img: QImage) -> tuple[int, int, int]:
    try:
        return (int(img.cacheKey()), int(img.width()), int(img.height()))
    except Exception:
        return (0, int(img.width()), int(img.height()))


def _import_qimage_to_media(img: QImage, prefix: str = "clipboard") -> str | None:
    if img.isNull() or not mw.col:
        return None

    ts = int(time.time() * 1000)
    filename = f"{prefix}_{ts}.png"
    abs_path = os.path.join(mw.col.media.dir(), filename)

    if not img.save(abs_path, "PNG"):
        return None

    try:
        mw.col.media.addFile(abs_path)
    except Exception:
        pass

    return filename


def _encode_masks(masks: list[dict]) -> str:
    payload = {"v": 1, "masks": masks}
    txt = json.dumps(payload, ensure_ascii=False)
    return base64.b64encode(txt.encode("utf-8")).decode("ascii")


def _decode_masks(b64: str) -> list[dict]:
    try:
        raw = base64.b64decode(b64.encode("ascii"), validate=False)
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        masks = obj.get("masks")
        if isinstance(masks, list):
            return cast(list[dict], masks)
    except Exception:
        pass
    return []


def _pack_internal(
    image_filename: str,
    group_id: str,
    active_index: int,
    masks: list[dict],
    mask_label: str = "",
) -> str:
    """
    Phase 1: store a consolidated internal JSON payload while keeping legacy fields.
    Stored as plain JSON string (not base64) for easier future migration/debug.
    """
    payload = {
        "v": 1,
        "image": image_filename or "",
        "group": group_id or "",
        "active": int(active_index),
        "masks": masks if isinstance(masks, list) else [],
        "mask_label": mask_label or "",
    }
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        # last-resort fallback
        return "{}"


def _unpack_internal(s: str) -> dict[str, Any] | None:
    if not s:
        return None
    t = (s or "").strip()
    if not t:
        return None
    try:
        obj = json.loads(t)
        if isinstance(obj, dict) and int(obj.get("v", 0) or 0) >= 1:
            return cast(dict[str, Any], obj)
    except Exception:
        return None
    return None


def _int_or0(s: Any) -> int:
    try:
        return int(str(s).strip())
    except Exception:
        return 0


def _media_data_url_scaled(filename: str, max_side: int = 1600, jpeg_quality: int = 85) -> str:
    abs_path = _media_abs_path(filename)

    img = QImage(abs_path)
    if img.isNull():
        with open(abs_path, "rb") as f:
            b = f.read()
        mime = _guess_mime(filename)
        b64 = base64.b64encode(b).decode("ascii")
        return f"data:{mime};base64,{b64}"

    w, h = img.width(), img.height()
    if max(w, h) > max_side:
        if w >= h:
            img = img.scaledToWidth(max_side, Qt.TransformationMode.SmoothTransformation)
        else:
            img = img.scaledToHeight(max_side, Qt.TransformationMode.SmoothTransformation)

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)

    if img.hasAlphaChannel():
        img.save(buf, "PNG")
        mime = "image/png"
    else:
        img.save(buf, "JPG", int(jpeg_quality))
        mime = "image/jpeg"

    buf.close()
    b64 = base64.b64encode(bytes(ba)).decode("ascii")
    return f"data:{mime};base64,{b64}"

def _img_proc_cfg(section: str, default_max_side: int, default_jpeg_quality: int) -> tuple[int, int]:
    """Read image-processing config for a given section under 05_image_processing.*."""
    max_side = int(_cfg_get(["05_image_processing", section, "max_side_px"], default_max_side))
    jpeg_quality = int(_cfg_get(["05_image_processing", section, "jpeg_quality"], default_jpeg_quality))
    # clamp to safe-ish ranges
    max_side = max(256, min(4096, max_side))
    jpeg_quality = max(30, min(95, jpeg_quality))
    return max_side, jpeg_quality


def _ai_image_bytes_scaled(filename: str, *, purpose: str) -> tuple[bytes, str]:
    """
    Build image bytes for AI request.

    - Does NOT modify the original media file.
    - Downscales only when larger than max_side_px.
    - Encodes as PNG if alpha is present, otherwise JPEG.

    purpose:
      - "ai_suggest"   : mask suggestions
      - "ai_metadata"  : title/explanation generation

    Returns: (bytes, mime)
    """
    abs_path = _media_abs_path(filename)

    # purpose-based scaling
    if purpose == "ai_metadata":
        max_side, jpeg_quality = _img_proc_cfg("ai_metadata", default_max_side=1024, default_jpeg_quality=80)
    else:
        max_side, jpeg_quality = _img_proc_cfg("ai_suggest", default_max_side=1024, default_jpeg_quality=80)

    img = QImage(abs_path)
    if img.isNull():
        # fallback: send original bytes (unknown image type or read error)
        with open(abs_path, "rb") as f:
            b = f.read()
        return b, _guess_mime(filename)

    w, h = img.width(), img.height()
    if max(w, h) > max_side:
        if w >= h:
            img = img.scaledToWidth(max_side, Qt.TransformationMode.SmoothTransformation)
        else:
            img = img.scaledToHeight(max_side, Qt.TransformationMode.SmoothTransformation)

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)

    if img.hasAlphaChannel():
        img.save(buf, "PNG")
        mime = "image/png"
    else:
        img.save(buf, "JPG", int(jpeg_quality))
        mime = "image/jpeg"

    buf.close()
    return bytes(ba), mime



# -------------------- AI sanitize / stabilize --------------------

_JSON_RE = re.compile(r"\{[\s\S]*\}|\[[\s\S]*\]")


def _extract_json(text: str) -> Any:
    m = _JSON_RE.search(text)
    if not m:
        raise ValueError("No JSON found in model output")
    return json.loads(m.group(0))


def _iou(a: dict, b: dict) -> float:
    ax0, ay0 = float(a["x"]), float(a["y"])
    ax1, ay1 = ax0 + float(a["w"]), ay0 + float(a["h"])
    bx0, by0 = float(b["x"]), float(b["y"])
    bx1, by1 = bx0 + float(b["w"]), by0 + float(b["h"])

    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0

    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _sanitize_masks(masks: list[dict], max_n: int = 12) -> list[dict]:
    cleaned: list[dict] = []
    for m in masks:
        try:
            x = float(m.get("x", 0))
            y = float(m.get("y", 0))
            w = float(m.get("w", 0))
            h = float(m.get("h", 0))
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(w) and math.isfinite(h)):
                continue
            if w <= 0 or h <= 0:
                continue

            x = max(0.0, min(1.0, x))
            y = max(0.0, min(1.0, y))
            w = max(0.001, min(1.0 - x, w))
            h = max(0.001, min(1.0 - y, h))

            if (w * h) < 0.0004:
                continue

            cleaned.append(
                {
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "label": str(m.get("label", ""))[:120],
                    "source": "ai",
                }
            )
        except Exception:
            continue

    cleaned.sort(key=lambda mm: (mm["w"] * mm["h"]), reverse=True)
    out: list[dict] = []
    for m in cleaned:
        if len(out) >= max_n:
            break
        keep = True
        for k in out:
            if _iou(m, k) >= 0.70:
                keep = False
                break
        if keep:
            out.append(m)

    return out


# -------------------- AI providers (optional) --------------------

def _openai_suggest(image_bytes: bytes, mime: str) -> list[dict]:
    api_key_env = _cfg_get(["04_ai", "openai", "api_key_env"], "OPENAI_API_KEY")
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise RuntimeError(f"Missing env: {api_key_env}")

    model = _cfg_get(["04_ai", "openai", "model"], "gpt-4.1-mini")
    max_n = int(_cfg_get(["04_ai", "max_suggestions"], 24))
    max_out = int(_cfg_get(["04_ai", "openai", "max_output_tokens"], 800))

    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "masks": {
                "type": "array",
                "maxItems": max_n,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "x": {"type": "number", "minimum": 0, "maximum": 1},
                        "y": {"type": "number", "minimum": 0, "maximum": 1},
                        "w": {"type": "number", "minimum": 0, "maximum": 1},
                        "h": {"type": "number", "minimum": 0, "maximum": 1},
                        "label": {"type": "string"},
                    },
                    "required": ["x", "y", "w", "h"],
                },
            },
        },
        "required": ["masks"],
    }

    prompt = (
        "You detect important regions to mask for study.\n"
        "Return ONLY JSON that matches the provided schema.\n"
        "Coordinates are normalized 0..1. Avoid tiny boxes and heavy overlaps.\n"
    )

    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
        "max_output_tokens": max_out,
        "temperature": 0.2,
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "mask_suggestions",
                "strict": True,
                "schema": schema,
            }
        },
    }

    req = urllib.request.Request(
        _cfg_get(["04_ai", "openai", "base_url"], "https://api.openai.com/v1/responses"),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=int(_cfg_get(["04_ai", "openai", "timeout_sec"], 45))) as resp:
        obj = json.loads(resp.read().decode("utf-8", errors="replace"))

    text = ""
    try:
        out = obj.get("output", [])
        for item in out:
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    text += c.get("text", "")
    except Exception:
        pass

    if not text:
        text = json.dumps(obj)

    try:
        j = json.loads(text)
    except Exception:
        j = _extract_json(text)

    masks = j.get("masks") if isinstance(j, dict) else None
    if not isinstance(masks, list):
        raise ValueError("No masks in output JSON")

    return cast(list[dict], masks)


def _gemini_suggest(image_bytes: bytes, mime: str) -> list[dict]:
    api_key_env = _cfg_get(["04_ai", "gemini", "api_key_env"], "GEMINI_API_KEY")
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise RuntimeError(f"Missing env: {api_key_env}")

    model = _cfg_get(["04_ai", "gemini", "model"], "gemini-2.5-flash")
    endpoint = _cfg_get(["04_ai", "gemini", "endpoint"], "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent")
    url = endpoint.replace("{model}", model) + f"?key={api_key}"
    max_out = int(_cfg_get(["04_ai", "gemini", "max_output_tokens"], 800))

    prompt = (
        "Return JSON only.\n"
        "Output format:\n"
        '{"masks":[{"x":0.1,"y":0.1,"w":0.2,"h":0.15,"label":"..."}]}\n'
        "All values normalized 0..1."
    )

    body = {
        "generationConfig": {"maxOutputTokens": max_out},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ]
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=int(_cfg_get(["04_ai", "gemini", "timeout_sec"], 45))) as resp:
        obj = json.loads(resp.read().decode("utf-8", errors="replace"))

    text = ""
    try:
        cands = obj.get("candidates", [])
        if cands:
            parts = cands[0]["content"]["parts"]
            for p in parts:
                if "text" in p:
                    text += p["text"]
    except Exception:
        pass
    if not text:
        text = json.dumps(obj)

    j = _extract_json(text)
    masks = j.get("masks") if isinstance(j, dict) else None
    if not isinstance(masks, list):
        raise ValueError("No masks in output JSON")
    return cast(list[dict], masks)


def suggest_masks_for_file(filename: str) -> list[dict]:
    image_bytes, mime = _ai_image_bytes_scaled(filename, purpose="ai_suggest")

    provider = _cfg_get(["04_ai", "provider"], "openai")
    if provider == "gemini":
        raw = _gemini_suggest(image_bytes, mime)
    else:
        raw = _openai_suggest(image_bytes, mime)

    max_n = int(_cfg_get(["04_ai", "max_suggestions"], 24))
    return _sanitize_masks(raw, max_n=max_n)


def generate_title_and_explanation(filename: str) -> dict[str, str]:
    """Generate English-only title + explanation from the image (optional feature)."""
    image_bytes, mime = _ai_image_bytes_scaled(filename, purpose="ai_metadata")

    provider = _cfg_get(["04_ai", "provider"], "openai")
    if provider == "gemini":
        return _gemini_gen_meta(image_bytes, mime)
    return _openai_gen_meta(image_bytes, mime)


def _openai_gen_meta(image_bytes: bytes, mime: str) -> dict[str, str]:
    api_key = os.environ.get(_cfg_get(["04_ai", "openai", "api_key_env"], "OPENAI_API_KEY"), "").strip()
    if not api_key:
        raise RuntimeError("Missing OpenAI API key (env).")

    url = _cfg_get(["04_ai", "openai", "base_url"], "https://api.openai.com/v1/responses")
    model = _cfg_get(["04_ai", "openai", "model"], "gpt-4.1-mini")
    timeout = int(_cfg_get(["04_ai", "openai", "timeout_sec"], 45))
    max_out = int(_cfg_get(["04_ai", "openai", "max_output_tokens"], 800))

    prompt = (
        "You are a medical study assistant. Create English-only:\n"
        "title (<=5 words), explanation (1-5 sentences).\n"
        "Return JSON only: {\"title\":\"...\",\"explanation\":\"...\"}."
    )

    body = {
        "model": model,
        "max_output_tokens": max_out,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:{mime};base64," + base64.b64encode(image_bytes).decode("ascii"),
                    },
                ],
            }
        ],
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTPError {e.code}: {raw}") from e

    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e

    except Exception as e:
        raise RuntimeError(f"Unexpected error during HTTP request: {e}") from e

    txt = ""
    for item in obj.get("output", []) or []:
        if item.get("type") == "message":
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    txt += c.get("text", "")

    if not txt:
        txt = json.dumps(obj)

    j = _extract_json(txt)
    if not isinstance(j, dict):
        raise RuntimeError("Failed to parse JSON from OpenAI meta output.")
    return {
        "title": str(j.get("title", "") or ""),
        "explanation": str(j.get("explanation", "") or ""),
    }


def _gemini_gen_meta(image_bytes: bytes, mime: str) -> dict[str, str]:
    api_key = os.environ.get(_cfg_get(["04_ai", "gemini", "api_key_env"], "GEMINI_API_KEY"), "").strip()
    if not api_key:
        raise RuntimeError("Missing Gemini API key (env).")

    model = _cfg_get(["04_ai", "gemini", "model"], "gemini-2.5-flash")
    endpoint = _cfg_get(["04_ai", "gemini", "endpoint"], "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent")
    url = endpoint.replace("{model}", str(model)) + f"?key={api_key}"
    timeout = int(_cfg_get(["04_ai", "gemini", "timeout_sec"], 45))
    max_out = int(_cfg_get(["04_ai", "gemini", "max_output_tokens"], 800))

    prompt = (
        "Return JSON only: {\"title\":\"...\",\"explanation\":\"...\"}.\n"
        "title <=10 words. explanation 1-5 sentences."
    )

    body = {
        "generationConfig": {"maxOutputTokens": max_out},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode("ascii")}},
                ],
            }
        ],
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTPError {e.code}: {raw}") from e

    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e

    except Exception as e:
        raise RuntimeError(f"Unexpected error during HTTP request: {e}") from e

    text = ""
    cands = obj.get("candidates", [])
    if cands:
        parts = cands[0].get("content", {}).get("parts", [])
        for p in parts:
            if "text" in p:
                text += p["text"]
    if not text:
        text = json.dumps(obj)

    j = _extract_json(text)
    if not isinstance(j, dict):
        raise RuntimeError("Failed to parse JSON from Gemini meta output.")
    return {
        "title": str(j.get("title", "") or ""),
        "explanation": str(j.get("explanation", "") or ""),
    }


# -------------------- dialog (singleton, no crash) --------------------

class MaskEditorDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.setWindowTitle("Image Masker")
        self.setMinimumSize(1100, 700)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self.mode: str = "create"  # "create" | "edit"
        self.existing_note_id: Optional[int] = None

        self.image_filename: str = ""
        self.group_id: str = ""
        self.masks: list[dict] = []

        self.title: str = ""
        self.explanation: str = ""

        self._web_ready = False
        self._hidden = True
        self._display_url_cache: dict[str, str] = {}

        self.web = AnkiWebView(kind=AnkiWebViewKind.EDITOR, parent=self)
        if hasattr(self.web, "set_bridge_command"):
            self.web.set_bridge_command(self._on_bridge_cmd, self)

        self.btnPick = QPushButton("Pick image…")
        self.btnAISuggest = QPushButton("AI suggest…")
        self.btnExport = QPushButton("Create cards")
        self.btnClose = QPushButton("Close")

        self.btnAISuggest.setEnabled(bool(_cfg_get(["04_ai", "enable_ai"], False)))

        self.status = QLabel("")
        self.status.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(self.btnPick)
        top.addWidget(self.btnAISuggest)
        top.addStretch(1)
        top.addWidget(self.btnExport)
        top.addWidget(self.btnClose)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.status)
        lay.addWidget(self.web, 1)

        self.btnPick.clicked.connect(self._pick_image)
        self.btnAISuggest.clicked.connect(self._ai_suggest)
        self.btnExport.clicked.connect(self._trigger_export)
        self.btnClose.clicked.connect(self._hide_only)

        self._load_editor_html()

        self._auto_wait_clipboard = False
        self._last_clip_sig: tuple[int, int, int] | None = None

        try:
            cb = QGuiApplication.clipboard()
            cb.dataChanged.connect(self._on_clipboard_changed)
        except Exception:
            pass

    def _load_editor_html(self) -> None:
        self._web_ready = False
        html = _build_editor_html_inline()
        self.web.setHtml(html, QUrl(mw.serverURL()))

    def closeEvent(self, ev) -> None:
        self._hide_only()
        ev.ignore()

    def _hide_only(self) -> None:
        self._hidden = True
        self.hide()

    def open_create(self) -> None:
        self._stop_wait_clipboard()
        self.mode = "create"
        self.existing_note_id = None
        self.btnExport.setText("Create cards")

        # reset only masks/meta (do NOT clear image_filename here)
        self.group_id = ""
        self.masks = []
        self.title = ""
        self.explanation = ""
        self._display_url_cache.clear()

        self._hidden = False
        self.show()
        self.raise_()
        self.activateWindow()
        self._push_state_to_js()
        QTimer.singleShot(80, self._push_state_to_js)
        QTimer.singleShot(250, self._push_state_to_js)
        QTimer.singleShot(900, self._push_state_to_js)

    def open_edit(self, note_id: int) -> None:
        self._stop_wait_clipboard()
        self.mode = "edit"
        self.existing_note_id = int(note_id)
        self.btnExport.setText("Save changes")
        self._load_existing()
        self._hidden = False
        self.show()
        self.raise_()
        self.activateWindow()
        self._push_state_to_js()
        QTimer.singleShot(80, self._push_state_to_js)
        QTimer.singleShot(250, self._push_state_to_js)
        QTimer.singleShot(900, self._push_state_to_js)

    def _load_existing(self) -> None:
        if not mw.col or not self.existing_note_id:
            return
        note = mw.col.get_note(self.existing_note_id)
        internal_raw = (note.get(FIELD_INTERNAL, "") if hasattr(note, "get") else note[FIELD_INTERNAL]) or ""
        internal = _unpack_internal(internal_raw)

        if internal:
            self.image_filename = str(internal.get("image", "") or "")
            self.group_id = str(internal.get("group", "") or "")
            masks_obj = internal.get("masks", [])
            self.masks = cast(list[dict], masks_obj) if isinstance(masks_obj, list) else []

            # fallback for older notes or partial payloads
            if not self.image_filename:
                self.image_filename = note[FIELD_IMAGEFILE] or ""
            if not self.group_id:
                self.group_id = note[FIELD_GROUPID] or ""
            if not self.masks:
                self.masks = _decode_masks(note[FIELD_MASKSB64] or "")
        else:
            self.image_filename = note[FIELD_IMAGEFILE] or ""
            self.group_id = note[FIELD_GROUPID] or ""
            self.masks = _decode_masks(note[FIELD_MASKSB64] or "")
        
        self.title = note.get(FIELD_TITLE, "") if hasattr(note, "get") else note[FIELD_TITLE] or ""
        self.explanation = note.get(FIELD_EXPLANATION, "") if hasattr(note, "get") else note[FIELD_EXPLANATION] or ""

        self.status.setText(f"Editing group: {self.group_id}\nImage: {self.image_filename}")

    def _pick_image(self) -> None:
        if not mw.col:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Pick an image",
            "",
            "Images (*.png *.jpg *.jpeg *.webp);;All files (*.*)",
        )
        if not path:
            return

        try:
            filename = mw.col.media.addFile(path)
        except Exception as e:
            showWarning(f"Failed to add media: {e}")
            return

        self.image_filename = filename

        # --- important: clear previous masks/group when changing image ---
        self.masks = []
        self.group_id = ""
        self.existing_note_id = None

        self.title = ""
        self.explanation = ""

        self._display_url_cache.clear()
        self.status.setText(f"Image loaded: {self.image_filename}")
        self._push_state_to_js()

    def _ai_suggest(self) -> None:
        if not bool(_cfg_get(["04_ai", "enable_ai"], False)):
            showInfo("AI is disabled in config (04_ai.enable_ai).")
            return
        if not self.image_filename:
            showInfo("Pick an image first.")
            return

        def work() -> list[dict]:
            return suggest_masks_for_file(self.image_filename)

        def done(fut) -> None:
            if self._hidden:
                return
            try:
                masks = fut.result()
            except Exception as e:
                showWarning(f"AI suggest failed: {e}")
                return

            msg = f"{len(masks)} suggestions."
            js = json.dumps(masks, ensure_ascii=False)
            self.web.eval(f"window.aioe && window.aioe.setSuggestions({js}, {json.dumps(msg)});")

        mw.taskman.run_in_background(work, done)

    def _trigger_export(self) -> None:
        try:
            self.web.eval("window.aioe && window.aioe.exportNow && window.aioe.exportNow();")
        except Exception as e:
            showWarning(f"Failed to trigger export: {e}")

    def _on_bridge_cmd(self, cmd: str) -> Any:
        if self._hidden:
            return

        if cmd.startswith("aioe:ready"):
            self._web_ready = True
            self._push_state_to_js()
            return

        if cmd.startswith("aioe:imgerr:"):
            src = cmd[len("aioe:imgerr:"):]
            showWarning("Image failed to load in WebView.\n\nSRC:\n" + src)
            return

        if cmd.startswith("aioe:genmeta:"):
            if not bool(_cfg_get(["04_ai", "enable_metadata_ai"], False)):
                try:
                    self.web.eval(
                        "window.aioe && window.aioe.setMetaStatus && "
                        'window.aioe.setMetaStatus("Disabled in config.");'
                    )
                except Exception:
                    pass
                showInfo("Metadata AI is disabled in config (04_ai.enable_metadata_ai).")
                return
            if not self.image_filename:
                showInfo("Pick an image first.")
                return

            def work():
                return generate_title_and_explanation(self.image_filename)

            def done(fut):
                if self._hidden:
                    return
                try:
                    meta = fut.result()
                except Exception:
                    try:
                        self.web.eval(
                            "window.aioe && window.aioe.setMetaStatus && "
                            'window.aioe.setMetaStatus("Failed.");'
                        )
                    except Exception:
                        pass
                    return
                self.title = meta.get("title", "")
                self.explanation = meta.get("explanation", "")
                js = json.dumps({**meta, "message": "Generated."}, ensure_ascii=False)
                self.web.eval(f"window.aioe && window.aioe.setMeta && window.aioe.setMeta({js});")

            mw.taskman.run_in_background(work, done)
            return

        if cmd.startswith("aioe:export:"):
            payload_enc = cmd[len("aioe:export:"):]
            try:
                import urllib.parse
                payload_txt = urllib.parse.unquote(payload_enc)
                payload = json.loads(payload_txt)
            except Exception as e:
                showWarning(f"Failed to parse export payload: {e}")
                return

            masks = payload.get("masks")
            if not isinstance(masks, list):
                showWarning("Export payload has no masks.")
                return

            self.masks = cast(list[dict], masks)
            meta = payload.get("meta") if isinstance(payload, dict) else None
            if isinstance(meta, dict):
                self.title = str(meta.get("title", "") or "")
                self.explanation = str(meta.get("explanation", "") or "")
            self._handle_export_payload()
            return

    def _get_display_url(self, filename: str) -> str:
        cached = self._display_url_cache.get(filename)
        if cached:
            return cached

        max_side = int(_cfg_get(["05_image_processing", "display", "max_side_px"], 1600))
        quality = int(_cfg_get(["05_image_processing", "display", "jpeg_quality"], 85))
        u = _media_data_url_scaled(filename, max_side=max_side, jpeg_quality=quality)
        self._display_url_cache[filename] = u
        return u

    def _push_state_to_js(self) -> None:
        if not self.image_filename:
            return

        try:
            url = self._get_display_url(self.image_filename)
        except Exception as e:
            showWarning(f"Failed to build display data URL: {e}")
            return

        masks_json_txt = json.dumps(self.masks, ensure_ascii=False)
        meta = {"title": self.title or "", "explanation": self.explanation or "", "message": ""}

        self.web.eval(f"""
    (function(){{
    const url = {json.dumps(url)};
    const masksTxt = {json.dumps(masks_json_txt)};
    const meta = {json.dumps(meta, ensure_ascii=False)};
    function go(){{
        if (!window.aioe || !window.aioe.setImage) return setTimeout(go, 30);
        try {{
        window.aioe.setImage(url);
        window.aioe.setMasks(JSON.parse(masksTxt));
        try {{ window.aioe.setMeta && window.aioe.setMeta(meta); }} catch(e) {{}}
        }} catch(e) {{}}
    }}
    go();
    }})();
    """)

    def _begin_wait_next_clipboard(self) -> None:
        self._auto_wait_clipboard = True
        tooltip("📋 Waiting for next clipboard image… (copy an image to continue)")

    def _stop_wait_clipboard(self) -> None:
        self._auto_wait_clipboard = False

    def _on_clipboard_changed(self) -> None:
        if not getattr(self, "_hidden", True):
            return
        if not self._auto_wait_clipboard:
            return
        if not mw.col:
            return

        try:
            cb = QGuiApplication.clipboard()
            img = cb.image()
            if img is None or img.isNull():
                return

            sig = _clipboard_image_signature(img)
            if self._last_clip_sig == sig:
                return

            fname = _import_qimage_to_media(img, prefix="clipboard")
            if not fname:
                return

            self._last_clip_sig = sig

            self.image_filename = fname
            self.title = ""
            self.explanation = ""
            self.masks = []
            self.group_id = ""
            self._display_url_cache.clear()
            self.status.setText("📋 Image imported from clipboard. Draw masks and Export.")
            self.open_create()
            self._push_state_to_js()

            self._stop_wait_clipboard()
        except Exception:
            return

    def _handle_export_payload(self) -> None:
        if not mw.col:
            return

        ensure_note_type()

        if not self.image_filename:
            showWarning("No image selected.")
            return
        if not self.masks:
            showWarning("No masks to export.")
            return

        if self.mode == "create":
            self.group_id = uuid.uuid4().hex[:12]
            self._create_notes_for_group(self.group_id, self.image_filename, self.masks)
            tooltip(f"Created {len(self.masks)} notes.")

            # --- clear session state after export ---
            self.image_filename = ""
            self.group_id = ""
            self.masks = []
            self.title = ""
            self.explanation = ""
            self._display_url_cache.clear()

            self._hide_only()
            self._begin_wait_next_clipboard()
            return

        if not self.group_id:
            showWarning("Missing GroupId on this note.")
            return

        self._sync_group_notes(self.group_id, self.image_filename, self.masks)
        tooltip("Group updated.")
        self._hide_only()
        self._begin_wait_next_clipboard()

    def _new_note(self):
        model_name = _cfg_get(["01_general", "note_type_name"], MODEL_NAME_DEFAULT) or MODEL_NAME_DEFAULT
        mm = mw.col.models
        m = mm.by_name(model_name)
        if not m:
            raise RuntimeError("Note type not found.")
        return mw.col.new_note(m)

    def _create_notes_for_group(self, group_id: str, image_filename: str, masks: list[dict]) -> None:
        deck_id = mw.col.decks.current()["id"]

        for i, m in enumerate(masks):
            note = self._new_note()

            note[FIELD_IMAGEFILE] = image_filename
            note[FIELD_IMAGEHTML] = f'<img class="aioe-img" src="{image_filename}">'
            note[FIELD_GROUPID] = group_id

            mask_label = (m.get("label", "") if isinstance(m, dict) else "")
            no = i + 1
            title_primary = (self.title or image_filename)

            note[FIELD_NO] = str(no)
            note[FIELD_SORTKEY] = f"{title_primary} #{no:03d}"
            note[FIELD_TITLE] = self.title
            note[FIELD_MASKLABEL] = mask_label
            note[FIELD_EXPLANATION] = self.explanation
            note[FIELD_INTERNAL] = _pack_internal(
                image_filename=image_filename,
                group_id=group_id,
                active_index=i,
                masks=masks,
                mask_label=mask_label,
            )

            mw.col.add_note(note, deck_id)

        mw.col.save()


    def _sync_group_notes(self, group_id: str, image_filename: str, masks: list[dict]) -> None:
        deck_id = mw.col.decks.current()["id"]

        nids = mw.col.find_notes(f'{FIELD_GROUPID}:"{group_id}"')
        by_idx: dict[int, int] = {}
        for nid in nids:
            note = mw.col.get_note(nid)
            no = _int_or0(note[FIELD_NO])
            if no <= 0:
                continue
            idx = no - 1
            by_idx[idx] = nid

        created = 0
        for i, m in enumerate(masks):
            if i in by_idx:
                note = mw.col.get_note(by_idx[i])
            else:
                note = self._new_note()
                note[FIELD_GROUPID] = group_id
                mw.col.add_note(note, deck_id)
                created += 1

            note[FIELD_IMAGEFILE] = image_filename
            note[FIELD_IMAGEHTML] = f'<img class="aioe-img" src="{image_filename}">'

            mask_label = (m.get("label", "") if isinstance(m, dict) else "")
            no = i + 1
            title_primary = (self.title or image_filename)

            note[FIELD_NO] = str(no)
            note[FIELD_SORTKEY] = f"{title_primary} #{no:03d}"
            note[FIELD_TITLE] = self.title
            note[FIELD_MASKLABEL] = mask_label
            note[FIELD_EXPLANATION] = self.explanation
            note[FIELD_INTERNAL] = _pack_internal(
                image_filename=image_filename,
                group_id=group_id,
                active_index=i,
                masks=masks,
                mask_label=mask_label,
            )
            note.flush()

        mw.col.save()
        if created:
            tooltip(f"Added {created} new notes.")

        extra_nids: list[int] = []
        for idx, nid in by_idx.items():
            if idx >= len(masks):
                extra_nids.append(nid)

        if extra_nids:
            msg = (
                f"This group has {len(extra_nids)} extra note(s).\n"
                f"(ActiveIndex >= {len(masks)})\n\n"
                "Delete them?"
            )
            if askUser(msg, parent=self):
                try:
                    self._remove_notes_safe(extra_nids)
                    mw.col.save()
                    tooltip("Extra notes deleted.")
                except Exception as e:
                    showWarning(f"Failed to delete extra notes: {e}")
            else:
                showInfo("Extra notes were not deleted.")


# -------------------- open dialog (singleton) -----------------

_DIALOG: Optional[MaskEditorDialog] = None


def _get_dialog() -> MaskEditorDialog:
    global _DIALOG
    if _DIALOG is None:
        _DIALOG = MaskEditorDialog(mw)
    return _DIALOG


def _open_create_dialog(_editor=None) -> None:
    if not bool(_cfg_get(["01_general", "enabled"], True)):
        tooltip("Image Masker is disabled in config.")
        return

    dlg = _get_dialog()

    fname = _import_image_from_clipboard()
    if fname:
        dlg.image_filename = fname
        dlg.title = ""
        dlg.explanation = ""
        dlg.masks = []
        dlg.group_id = ""
        dlg._display_url_cache.clear()
        dlg.status.setText("📋 Image imported from clipboard.")
        dlg.open_create()
        dlg._push_state_to_js()
        return

    dlg.open_create()


def _open_edit_dialog(editor) -> None:
    try:
        note = getattr(editor, "note", None)
        if not note:
            return
        model_name = _cfg_get(["01_general", "note_type_name"], MODEL_NAME_DEFAULT) or MODEL_NAME_DEFAULT
        nid = int(getattr(note, "id", 0) or 0)
        if nid <= 0:
            _open_create_dialog(editor)
            return
        if note.note_type()["name"] != model_name:
            showWarning("This note is not an Image Masker note.")
            return
        _get_dialog().open_edit(nid)
    except Exception as e:
        showWarning(f"Failed to open editor: {e}")


def _open_from_editor(editor) -> None:
    try:
        note = getattr(editor, "note", None)
        model_name = _cfg_get(["01_general", "note_type_name"], MODEL_NAME_DEFAULT) or MODEL_NAME_DEFAULT
        nid = int(getattr(note, "id", 0) or 0)
        if note and nid > 0 and note.note_type()["name"] == model_name:
            _open_edit_dialog(editor)
            return
    except Exception:
        pass
    _open_create_dialog(editor)


def _on_editor_init_buttons(buttons, editor) -> None:
    if not bool(_cfg_get(["02_editor", "add_editor_button"], True)):
        return

    label = _cfg_get(["02_editor", "editor_button_label"], "🖼️") or "🖼️"
    tip = _cfg_get(["02_editor", "editor_button_tooltip"], "Create / edit image occlusion notes") or "Create / edit image occlusion notes"

    try:
        btn = editor.addButton(
            icon=None,
            cmd="image_masker",
            func=lambda e=editor: _open_from_editor(e),
            label=label,
            tip=tip,
        )
    except TypeError:
        btn = editor.addButton(None, "image_masker", lambda e=editor: _open_from_editor(e), tip=tip, label=label)
    except Exception:
        return

    try:
        buttons.append(btn)
    except Exception:
        pass


def _init() -> None:
    try:
        ensure_note_type()
    except Exception as e:
        showWarning(f"Failed to ensure note type: {e}")

    _install_config_action()
    gui_hooks.editor_did_init_buttons.append(_on_editor_init_buttons)

    # Invalidate cache when addon config is saved (hook names vary by Anki version)
    for hook_name in (
        "addon_config_editor_will_save_json",
        "addon_config_editor_did_save_json",
        "addon_config_editor_will_save",
        "addon_config_editor_did_save",
    ):
        hook = getattr(gui_hooks, hook_name, None)
        if hook is None:
            continue

        try:
            if hook_name.endswith("_will_save_json"):
                hook.append(_invalidate_config_cache_keep_json)
            else:
                hook.append(_invalidate_config_cache)
        except Exception:
            pass

_init()
