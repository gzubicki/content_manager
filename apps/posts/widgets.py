"""Custom admin widgets for post editing helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django import forms
from django.forms.utils import flatatt
from django.utils.html import format_html
from django.utils.safestring import mark_safe

DATETIME_VALUE_FORMAT = "%Y-%m-%d %H:%M"
DATETIME_DISPLAY_FORMAT = "%d.%m.%Y %H:%M"
DATETIME_INPUT_FORMATS = [
    DATETIME_VALUE_FORMAT,
    "%Y-%m-%dT%H:%M",
    "%d.%m.%Y %H:%M",
]


@dataclass
class _AttrExtraction:
    widget_attrs: dict[str, Any]
    surface_attrs: dict[str, Any]


def _extract_editor_attrs(attrs: dict[str, Any]) -> _AttrExtraction:
    """Split attributes between the hidden textarea and visual surface."""

    widget_attrs: dict[str, Any] = {**attrs}
    surface_attrs: dict[str, Any] = {
        "data-admin-editor": "1",
    }
    for key in list(widget_attrs.keys()):
        if key.startswith("data-editor-"):
            surface_attrs[key] = widget_attrs.pop(key)
    return _AttrExtraction(widget_attrs, surface_attrs)


class PostTextWidget(forms.Textarea):
    """Textarea backed by a Quill editor surface."""

    def __init__(self, attrs: dict[str, Any] | None = None):
        base_attrs = {"rows": 14}
        if attrs:
            base_attrs.update(attrs)
        super().__init__(attrs=base_attrs)

    def render(self, name, value, attrs: dict[str, Any] | None = None, renderer=None):  # type: ignore[override]
        attrs = {**(attrs or {})}
        extracted = _extract_editor_attrs({**self.attrs, **attrs})
        defaults = extracted.widget_attrs
        surface_attrs = extracted.surface_attrs
        input_id = defaults.get("id") or f"id_{name}"
        defaults.setdefault("class", "")
        defaults["class"] = f"{defaults['class']} admin-editor__input".strip()
        defaults["data-post-text-source"] = "1"
        defaults.setdefault("autocomplete", "off")
        textarea_html = super().render(name, value, defaults, renderer)

        surface_attrs.setdefault("id", f"{input_id}__surface")
        surface_attrs["data-editor-input"] = f"#{input_id}"
        surface_attrs.setdefault("class", "admin-editor__surface")
        surface_attr_html = flatatt(surface_attrs)

        meta_html = (
            "<div class=\"admin-editor__meta\">"
            "<span data-admin-editor-count>0</span>"
            "<span class=\"admin-editor__meta-unit\">znaków</span>"
            "</div>"
        )
        wrapper = format_html(
            '<div class="admin-editor" data-editor-container data-editor-ready="0">'
            "{textarea}<div{surface}></div>{meta}</div>",
            textarea=mark_safe(textarea_html),
            surface=mark_safe(surface_attr_html),
            meta=mark_safe(meta_html),
        )
        return mark_safe(wrapper)


class DateTimePickerWidget(forms.DateTimeInput):
    """Single input datetime picker integrated with Flatpickr."""

    def __init__(self, attrs: dict[str, Any] | None = None, format: str = DATETIME_VALUE_FORMAT):
        base_attrs = {
            "data-date-format": "Y-m-d H:i",
            "data-alt-format": "d.m.Y H:i",
            "data-locale": "pl",
            "data-admin-datetime-input": "1",
            "data-post-datetime-source": "1",
            "data-alt-placeholder": "np. 31.12.2023 18:30",
            "autocomplete": "off",
            "placeholder": "2023-12-31 18:30",
            "inputmode": "numeric",
        }
        if attrs:
            base_attrs.update(attrs)
        super().__init__(attrs=base_attrs, format=format)

    def render(self, name, value, attrs: dict[str, Any] | None = None, renderer=None):  # type: ignore[override]
        attrs = {**(attrs or {})}
        defaults = {**self.attrs, **attrs}
        defaults.setdefault("class", "")
        defaults["class"] = f"{defaults['class']} admin-datetime__input".strip()
        input_html = super().render(name, value, defaults, renderer)
        wrapper = format_html(
            '<div class="admin-datetime" data-admin-datetime data-datetime-ready="0">'
            '<span class="admin-datetime__icon" aria-hidden="true">📅</span>'
            "{input}<button type=\"button\" class=\"admin-datetime__clear\" data-datetime-clear"
            " aria-label=\"Wyczyść termin\">✕</button></div>",
            input=mark_safe(input_html),
        )
        return mark_safe(wrapper)


__all__ = [
    "DateTimePickerWidget",
    "PostTextWidget",
    "DATETIME_INPUT_FORMATS",
    "DATETIME_VALUE_FORMAT",
    "DATETIME_DISPLAY_FORMAT",
]
