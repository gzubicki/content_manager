(function(){
  const telegramLimits = {
    text: 4096
  };

  function setCaret(textarea, start, end){
    textarea.focus();
    textarea.setSelectionRange(start, end);
  }

  function surroundSelection(textarea, before, after, placeholder){
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const value = textarea.value;
    const hasSelection = start !== end;
    const insert = hasSelection ? value.slice(start, end) : (placeholder || "");
    const nextValue = value.slice(0, start) + before + insert + after + value.slice(end);
    const cursor = start + before.length + insert.length;
    textarea.value = nextValue;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    setCaret(textarea, cursor, cursor);
  }

  function insertLink(textarea){
    const url = window.prompt("Podaj adres URL (https://...)");
    if (!url){
      return;
    }
    const label = textarea.selectionStart !== textarea.selectionEnd
      ? textarea.value.slice(textarea.selectionStart, textarea.selectionEnd)
      : window.prompt("Tekst linku", "link");
    const safeLabel = label || "link";
    const mark = `[${safeLabel}](${url})`;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const value = textarea.value;
    textarea.value = value.slice(0, start) + mark + value.slice(end);
    const caret = start + mark.length;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    setCaret(textarea, caret, caret);
  }

  function stripFormatting(value){
    return value
      .replace(/\*\*(.*?)\*\*/gs, "$1")
      .replace(/__(.*?)__/gs, "$1")
      .replace(/_(.*?)_/gs, "$1")
      .replace(/\*(.*?)\*/gs, "$1")
      .replace(/~~(.*?)~~/gs, "$1")
      .replace(/```([\s\S]*?)```/g, "$1")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1 ($2)");
  }

  function clearFormatting(textarea){
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const value = textarea.value;
    if (start !== end){
      const selected = value.slice(start, end);
      const cleaned = stripFormatting(selected);
      textarea.value = value.slice(0, start) + cleaned + value.slice(end);
      const caret = start + cleaned.length;
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      setCaret(textarea, caret, caret);
    } else {
      const cleaned = stripFormatting(value);
      textarea.value = cleaned;
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      const caret = Math.min(start, cleaned.length);
      setCaret(textarea, caret, caret);
    }
  }

  function surroundCodeBlock(textarea){
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const value = textarea.value;
    const selected = value.slice(start, end) || "kod";
    const insert = "```\n" + selected + "\n```";
    textarea.value = value.slice(0, start) + insert + value.slice(end);
    const caret = start + insert.length;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    setCaret(textarea, caret, caret);
  }

  function autoResize(textarea){
    textarea.style.height = "auto";
    const maxHeight = textarea.dataset.tgMaxHeight ? parseInt(textarea.dataset.tgMaxHeight, 10) : 720;
    textarea.style.height = Math.min(textarea.scrollHeight + 2, maxHeight) + "px";
  }

  function updateCounter(wrapper, textarea){
    const counter = wrapper.querySelector("[data-tg-editor-counter]");
    if (!counter){
      return;
    }
    const length = textarea.value.length;
    counter.textContent = `${length} / ${telegramLimits.text}`;
    if (length > telegramLimits.text){
      counter.dataset.state = "overflow";
    } else {
      counter.dataset.state = "ok";
    }
  }

  function initTextEditor(textarea){
    if (!textarea || textarea.dataset.tgEditorInit){
      return;
    }
    textarea.dataset.tgEditorInit = "1";
    const compact = textarea.dataset.tgEditorCompact === "1";

    const parent = textarea.parentElement;
    if (!parent){
      return;
    }

    const wrapper = document.createElement("div");
    wrapper.className = "tg-editor";
    if (compact){
      wrapper.setAttribute("data-compact", "1");
    }

    const toolbar = document.createElement("div");
    toolbar.className = "tg-editor__toolbar";

    const area = document.createElement("div");
    area.className = "tg-editor__area";

    const counter = document.createElement("p");
    counter.className = "tg-editor__counter";
    counter.setAttribute("data-tg-editor-counter", "1");
    counter.textContent = `0 / ${telegramLimits.text}`;

    parent.insertBefore(wrapper, textarea);
    wrapper.appendChild(toolbar);
    wrapper.appendChild(area);
    area.appendChild(textarea);
    wrapper.appendChild(counter);

    textarea.classList.add("tg-editor__textarea");
    textarea.dataset.tgMaxHeight = compact ? "420" : "720";

    const buttons = [
      { key: "bold", label: "B", title: "Pogrubienie (Markdown)", action: function(){ surroundSelection(textarea, "**", "**", "tekst"); } },
      { key: "italic", label: "I", title: "Kursywa (Markdown)", action: function(){ surroundSelection(textarea, "_", "_", "tekst"); } },
      { key: "strike", label: "S", title: "Przekreślenie", action: function(){ surroundSelection(textarea, "~~", "~~", "tekst"); } },
      { key: "code", label: "<>", title: "Kod inline", action: function(){ surroundSelection(textarea, "`", "`", "kod"); } },
      { key: "codeblock", label: "{ }", title: "Blok kodu", action: function(){ surroundCodeBlock(textarea); } },
      { key: "link", label: "🔗", title: "Dodaj link (Markdown)", action: function(){ insertLink(textarea); } },
      { key: "clear", label: "CLR", title: "Wyczyść formatowanie", css: "is-secondary", action: function(){ clearFormatting(textarea); } }
    ];

    buttons.forEach(function(btn){
      const button = document.createElement("button");
      button.type = "button";
      button.className = "tg-editor__button";
      if (btn.css){
        button.classList.add(btn.css);
      }
      button.textContent = btn.label;
      button.title = btn.title;
      button.addEventListener("click", function(event){
        event.preventDefault();
        btn.action();
        autoResize(textarea);
        updateCounter(wrapper, textarea);
      });
      toolbar.appendChild(button);
    });

    const enforceLimit = function(event){
      const value = textarea.value;
      if (value.length > telegramLimits.text){
        textarea.value = value.slice(0, telegramLimits.text);
        if (event){
          event.preventDefault();
        }
      }
    };

    textarea.addEventListener("input", function(event){
      enforceLimit(event);
      autoResize(textarea);
      updateCounter(wrapper, textarea);
    });

    textarea.addEventListener("change", function(event){
      enforceLimit(event);
      autoResize(textarea);
      updateCounter(wrapper, textarea);
    });

    window.addEventListener("resize", function(){
      window.requestAnimationFrame(function(){
        autoResize(textarea);
      });
    });

    autoResize(textarea);
    updateCounter(wrapper, textarea);
  }

  function parseFromInputs(dateInput, timeInput){
    const dateValue = (dateInput.value || "").trim();
    const timeValue = (timeInput.value || "").trim();
    if (!dateValue){
      return null;
    }
    const iso = `${dateValue}T${timeValue || "00:00"}`;
    const candidate = new Date(iso);
    if (Number.isNaN(candidate.getTime())){
      return null;
    }
    return candidate;
  }

  function formatForInputs(date){
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, "0");
    const d = String(date.getDate()).padStart(2, "0");
    const h = String(date.getHours()).padStart(2, "0");
    const min = String(date.getMinutes()).padStart(2, "0");
    const s = String(date.getSeconds()).padStart(2, "0");
    return {
      date: `${y}-${m}-${d}`,
      time: `${h}:${min}:${s}`
    };
  }

  function formatHuman(date){
    return date.toLocaleString(undefined, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    });
  }

  function initDatePicker(){
    const dateInput = document.getElementById("id_scheduled_at_0");
    const timeInput = document.getElementById("id_scheduled_at_1");
    if (!dateInput || !timeInput){
      return;
    }
    if (typeof Vue === "undefined" || typeof VueDatePicker === "undefined"){
      console.warn("Vue 3 DatePicker not available");
      return;
    }

    const row = dateInput.closest(".form-row") || dateInput.parentElement;
    if (!row){
      return;
    }

    const datetimeWrapper = dateInput.closest(".datetime");
    if (datetimeWrapper){
      datetimeWrapper.classList.add("tg-hidden-inputs");
    } else {
      dateInput.style.display = "none";
      timeInput.style.display = "none";
    }

    const mountPoint = document.createElement("div");
    mountPoint.className = "tg-datetime-picker";
    mountPoint.setAttribute("data-tg-datepicker", "1");
    const help = row.querySelector(".help");
    row.insertBefore(mountPoint, help);

    const initial = parseFromInputs(dateInput, timeInput);
    const { createApp, ref, computed, watch } = Vue;
    const schedulePicker = {
      setManual: function(){}
    };

    const app = createApp({
      components: {
        Datepicker: VueDatePicker
      },
      setup: function(){
        const modelValue = ref(initial);
        const manualMode = ref(true);
        const disabled = computed(function(){ return !manualMode.value; });
        const humanPreview = computed(function(){
          if (!modelValue.value){
            return "Brak wybranej daty";
          }
          return formatHuman(modelValue.value);
        });

        watch(modelValue, function(next){
          if (!next){
            dateInput.value = "";
            timeInput.value = "";
          } else {
            const formatted = formatForInputs(next);
            dateInput.value = formatted.date;
            timeInput.value = formatted.time;
          }
          dateInput.dispatchEvent(new Event("input", { bubbles: true }));
          timeInput.dispatchEvent(new Event("input", { bubbles: true }));
        });

        schedulePicker.setManual = function(isManual){
          manualMode.value = !!isManual;
          if (!manualMode.value){
            modelValue.value = null;
          }
        };

        schedulePicker.getManual = function(){
          return manualMode.value;
        };

        schedulePicker.setFromInputs = function(){
          const parsed = parseFromInputs(dateInput, timeInput);
          modelValue.value = parsed;
        };

        const pickNow = function(){
          const now = new Date();
          modelValue.value = now;
        };

        const clearAll = function(){
          modelValue.value = null;
        };

        return {
          modelValue,
          disabled,
          humanPreview,
          pickNow,
          clearAll
        };
      },
      template: `
        <div :class="['tg-datetime-picker__inner']">
          <div class="tg-datetime-picker__header">
            <p class="tg-datetime-picker__title">Termin publikacji</p>
            <span class="tg-datetime-picker__status">{{ humanPreview }}</span>
          </div>
          <date-picker
            v-model="modelValue"
            :is-24="true"
            :enable-time-picker="true"
            :time-picker-inline="true"
            :auto-apply="true"
            :clearable="false"
            :teleport="false"
            :allow-prevent-default="true"
            placeholder="Wybierz datę i godzinę"
            locale="pl"
          ></date-picker>
          <div class="tg-datetime-picker__actions">
            <button type="button" class="tg-datetime-picker__link" @click="pickNow" :disabled="disabled">Teraz</button>
            <button type="button" class="tg-datetime-picker__link is-muted" @click="clearAll" :disabled="disabled">Wyczyść</button>
          </div>
        </div>
      `
    });

    app.mount(mountPoint);

    const scheduleModeSelect = document.getElementById("id_schedule_mode");
    const syncManual = function(){
      const manual = !scheduleModeSelect || scheduleModeSelect.value === "MANUAL";
      schedulePicker.setManual(manual);
      mountPoint.classList.toggle("is-disabled", !manual);
      if (!manual){
        dateInput.value = "";
        timeInput.value = "";
        dateInput.dispatchEvent(new Event("input", { bubbles: true }));
        timeInput.dispatchEvent(new Event("input", { bubbles: true }));
      } else {
        schedulePicker.setFromInputs();
      }
    };

    if (scheduleModeSelect){
      scheduleModeSelect.addEventListener("change", syncManual);
    }
    syncManual();

    window.tgSchedulePicker = schedulePicker;
    if (initial){
      schedulePicker.setFromInputs();
    }
  }

  document.addEventListener("DOMContentLoaded", function(){
    document.querySelectorAll("textarea[data-tg-editor]").forEach(initTextEditor);
    initDatePicker();
  });
})();
