from __future__ import annotations

from datetime import datetime
from typing import Any

from django.contrib.admin.widgets import AdminSplitDateTime
from django.forms.utils import flatatt
from django.forms.widgets import HiddenInput
from django.utils.safestring import mark_safe


class FlatpickrSplitDateTimeWidget(AdminSplitDateTime):
    template_name = "admin/widgets/flatpickr_split_datetime.html"
    display_format = "%Y-%m-%d %H:%M"

    class Media:
        css = {"all": ("vendor/flatpickr/flatpickr.min.css",)}
        js = ("vendor/flatpickr/flatpickr.min.js", "admin/datetime_picker.js")

    def format_flat_value(self, value: Any) -> str:
        values = super().format_value(value)
        if not values:
            return ""
        try:
            date_part, time_part = values
        except (TypeError, ValueError):
            return ""
        date_part = (date_part or "").strip()
        time_part = (time_part or "").strip()
        if not date_part and not time_part:
            return ""
        if not time_part:
            return date_part
        if not date_part:
            return time_part
        return f"{date_part} {time_part}"

    def get_context(self, name: str, value: Any, attrs: dict[str, Any] | None) -> dict[str, Any]:
        attrs = attrs.copy() if attrs else {}
        css_classes = [cls for cls in attrs.get("class", "").split() if cls]
        for extra in ("vTextField", "js-datetime-picker"):
            if extra not in css_classes:
                css_classes.append(extra)
        if css_classes:
            attrs["class"] = " ".join(css_classes)
        attrs.setdefault("id", f"id_{name}_flatpickr")
        attrs.setdefault("autocomplete", "off")

        context = super().get_context(name, value, attrs)
        widget = context["widget"]
        subwidgets = list(widget.get("subwidgets", []))
        widget["subwidgets"] = subwidgets
        widget["flatpickr_value"] = self.format_flat_value(value)
        widget["combined_name"] = f"{name}_flatpickr"
        widget["date_id"] = self._extract_subwidget_id(subwidgets, 0)
        widget["time_id"] = self._extract_subwidget_id(subwidgets, 1)
        widget["attrs"] = widget.get("attrs", {})
        widget["attrs"].update(attrs)
        widget["flat_attrs"] = flatatt(widget["attrs"])
        widget["hidden_inputs"] = [
            mark_safe(self._render_hidden_subwidget(subwidget))
            for subwidget in subwidgets
        ]
        return context

    @staticmethod
    def _extract_subwidget_id(subwidgets: list[Any], index: int) -> str:
        if len(subwidgets) <= index:
            return ""
        subwidget = subwidgets[index]
        if hasattr(subwidget, "id_for_label"):
            return getattr(subwidget, "id_for_label")
        attrs = getattr(subwidget, "attrs", None)
        if isinstance(attrs, dict):
            return attrs.get("id", "")
        if isinstance(subwidget, dict):
            attrs = subwidget.get("attrs")
            if isinstance(attrs, dict):
                return attrs.get("id", "")
        return ""

    def _render_hidden_subwidget(self, subwidget: Any) -> str:
        attrs: dict[str, Any] = {}
        name: str | None = None
        value: Any = None

        if hasattr(subwidget, "as_widget"):
            return subwidget.as_widget(
                attrs={"type": "hidden", "data-flatpickr-hidden": "1"}
            )

        if isinstance(subwidget, dict):
            attrs = subwidget.get("attrs") or {}
            name = subwidget.get("name")
            value = subwidget.get("value")
        elif hasattr(subwidget, "data"):
            data = getattr(subwidget, "data") or {}
            attrs = data.get("attrs") or {}
            name = data.get("name")
            value = data.get("value")

        attrs = {**attrs, "data-flatpickr-hidden": "1"}
        # Usuwamy klasę prezentacyjną, aby ukryte pola nie były stylowane jak pola tekstowe.
        attrs.pop("class", None)

        return HiddenInput().render(name or "", value, attrs=attrs)

    def value_from_datadict(self, data: dict[str, Any], files: dict[str, Any], name: str) -> list[str]:
        combined_key = f"{name}_flatpickr"
        combined_value = (data.get(combined_key) or "").strip()
        if combined_value:
            normalized = combined_value.replace("T", " ").split()
            if len(normalized) >= 2:
                date_part = normalized[0]
                time_part = normalized[1]
                return [date_part, time_part]
            try:
                parsed = datetime.strptime(combined_value, self.display_format)
            except ValueError:
                pass
            else:
                return [parsed.strftime("%Y-%m-%d"), parsed.strftime("%H:%M")]
        return super().value_from_datadict(data, files, name)

    def use_required_attribute(self, initial: Any) -> bool:
        # Wymagane pole obsłuży Flatpickr – ukryte inputy pozostają opcjonalne.
        return False


__all__ = ["FlatpickrSplitDateTimeWidget"]
