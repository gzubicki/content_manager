(function(){
  "use strict";

  const EMOJI_REGEX = /\p{Emoji}/gu;

  function parseChannelMetadata(){
    const node = document.getElementById("channel-metadata");
    if (!node){
      return {};
    }
    try {
      const payload = JSON.parse(node.textContent || "[]");
      return (payload || []).reduce((acc, item) => {
        if (item && item.id != null){
          acc[String(item.id)] = item;
        }
        return acc;
      }, {});
    } catch(err){
      console.warn("Nie można odczytać metadanych kanałów", err);
      return {};
    }
  }

  function buildButton(config){
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tg-editor__btn";
    btn.innerHTML = config.html || config.label;
    btn.title = config.title || config.label;
    btn.setAttribute("aria-label", config.title || config.label);
    btn.addEventListener("click", config.onClick, false);
    return btn;
  }

  function wrapSelection(textarea, prefix, suffix, placeholder){
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? start;
    const value = textarea.value;
    const selected = value.slice(start, end) || placeholder || "";
    const before = value.slice(0, start);
    const after = value.slice(end);
    const injected = prefix + selected + suffix;
    textarea.value = before + injected + after;
    const selectionStart = before.length + prefix.length;
    const selectionEnd = selectionStart + selected.length;
    textarea.focus();
    textarea.setSelectionRange(selectionStart, selectionEnd);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function insertLink(textarea){
    const url = window.prompt("Adres URL", "https://");
    if (!url){
      return;
    }
    wrapSelection(textarea, "[", `](${url.trim()})`, "tekst");
  }

  function insertNewLine(textarea){
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? start;
    const before = textarea.value.slice(0, start);
    const after = textarea.value.slice(end);
    textarea.value = before + "\n" + after;
    const pos = before.length + 1;
    textarea.focus();
    textarea.setSelectionRange(pos, pos);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function computeEmojiCount(value){
    if (!value){
      return 0;
    }
    const matches = value.match(EMOJI_REGEX);
    return matches ? matches.length : 0;
  }

  function initTextEditor(channelMetadata){
    const editors = document.querySelectorAll("textarea[data-telegram-editor]");
    if (!editors.length){
      return;
    }

    const channelSelect = document.getElementById("id_channel");

    editors.forEach((textarea) => {
      if (textarea.dataset.telegramEditorReady){
        return;
      }
      textarea.dataset.telegramEditorReady = "1";

      const wrapper = document.createElement("div");
      wrapper.className = "tg-editor";

      const toolbar = document.createElement("div");
      toolbar.className = "tg-editor__toolbar";

      const counter = document.createElement("div");
      counter.className = "tg-editor__counter";

      const buttons = [
        { label: "B", title: "Pogrubienie (**tekst**)", onClick: () => wrapSelection(textarea, "**", "**", "pogrubienie") },
        { label: "I", title: "Kursywa (_tekst_)", onClick: () => wrapSelection(textarea, "_", "_", "kursywa") },
        { label: "U", title: "Podkreślenie (__tekst__)", onClick: () => wrapSelection(textarea, "__", "__", "podkreślenie") },
        { label: "S", title: "Przekreślenie (~tekst~)", onClick: () => wrapSelection(textarea, "~", "~", "przekreśl") },
        { html: "<code>Mono</code>", title: "Kod (`fragment`)", onClick: () => wrapSelection(textarea, "`", "`", "kod") },
        { html: "<code>Blok</code>", title: "Kod blokowy (```)", onClick: () => wrapSelection(textarea, "```\n", "\n```", "kod") },
        { label: "Spoiler", title: "Spoiler (||tekst||)", onClick: () => wrapSelection(textarea, "||", "||", "sekret") },
        { label: "Link", title: "Wstaw link", onClick: () => insertLink(textarea) },
        { label: "↵", title: "Nowa linia", onClick: () => insertNewLine(textarea) }
      ];

      buttons.forEach((config) => toolbar.appendChild(buildButton(config)));

      const parent = textarea.parentNode;
      parent.insertBefore(wrapper, textarea);
      wrapper.appendChild(toolbar);
      wrapper.appendChild(textarea);
      wrapper.appendChild(counter);

      function currentChannelMeta(){
        if (!channelSelect){
          return null;
        }
        return channelMetadata[String(channelSelect.value)] || null;
      }

      function updateCounter(){
        const meta = currentChannelMeta();
        const limit = meta && meta.max_chars ? Number(meta.max_chars) : null;
        const emojiMin = meta && meta.emoji_min ? Number(meta.emoji_min) : null;
        const emojiMax = meta && meta.emoji_max ? Number(meta.emoji_max) : null;
        const value = textarea.value || "";
        const length = value.length;
        const emojiCount = computeEmojiCount(value);

        const parts = [];
        parts.push(`Znaki: ${limit ? `${length}/${limit}` : length}`);
        if (limit && length > limit){
          parts.push("⚠️ ponad limit");
          counter.dataset.overLimit = "1";
        } else {
          delete counter.dataset.overLimit;
        }

        if (emojiMin || emojiMax){
          const range = `${emojiMin ?? 0}-${emojiMax ?? "∞"}`;
          const hint = emojiMin && emojiCount < emojiMin ? " (za mało)" : (emojiMax && emojiCount > emojiMax ? " (za dużo)" : "");
          parts.push(`Emoji: ${emojiCount}${range ? ` (zalecane ${range})` : ""}${hint}`);
        }

        counter.textContent = parts.join(" • ");
      }

      textarea.addEventListener("input", updateCounter);
      if (channelSelect){
        channelSelect.addEventListener("change", updateCounter);
      }
      updateCounter();
    });
  }

  function formatDateValue(date){
    const pad = (num) => String(num).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function formatTimeValue(date){
    const pad = (num) => String(num).padStart(2, "0");
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function parseExistingDate(dateInput, timeInput){
    const dateValue = (dateInput.value || "").trim();
    const timeValue = (timeInput.value || "").trim();
    if (!dateValue){
      return null;
    }
    const timePart = timeValue || "00:00";
    const candidate = new Date(`${dateValue}T${timePart}`);
    return Number.isNaN(candidate.getTime()) ? null : candidate;
  }

  function initDatePickers(){
    if (!window.Vue || !window.VueDatePicker){
      return;
    }
    const dateInputs = document.querySelectorAll('input[id$="scheduled_at_0"]');
    dateInputs.forEach((dateInput) => {
      if (dateInput.dataset.tgDatepickerMounted){
        return;
      }
      const timeInput = document.getElementById(dateInput.id.replace(/_0$/, "_1"));
      if (!timeInput){
        return;
      }

      const parent = dateInput.parentNode;
      if (!parent){
        return;
      }

      const host = document.createElement("div");
      host.className = "tg-date-picker";
      parent.insertBefore(host, dateInput);

      dateInput.dataset.tgDatepickerMounted = "1";
      timeInput.dataset.tgDatepickerMounted = "1";
      dateInput.type = "hidden";
      timeInput.type = "hidden";

      const initial = parseExistingDate(dateInput, timeInput);

      const app = window.Vue.createApp({
        components: { VueDatePicker: window.VueDatePicker },
        data(){
          return {
            modelValue: initial,
            actionRow: {
              showNowButton: true,
              nowButtonLabel: "Teraz",
              showCancel: true,
              cancelButtonLabel: "Wyczyść"
            }
          };
        },
        methods: {
          formatDisplay(date){
            if (!date){
              return "Wybierz termin";
            }
            return new Intl.DateTimeFormat("pl-PL", {
              year: "numeric",
              month: "2-digit",
              day: "2-digit",
              hour: "2-digit",
              minute: "2-digit"
            }).format(date);
          },
          updateInputs(value){
            if (!value){
              dateInput.value = "";
              timeInput.value = "";
            } else {
              const local = new Date(value);
              dateInput.value = formatDateValue(local);
              timeInput.value = formatTimeValue(local);
            }
            const evt = new Event("input", { bubbles: true });
            dateInput.dispatchEvent(evt);
            timeInput.dispatchEvent(evt);
          },
          handleNow(){
            const now = new Date();
            this.modelValue = now;
          },
          handleClear(){
            this.modelValue = null;
          }
        },
        watch: {
          modelValue: {
            handler(value){
              this.updateInputs(value);
            },
            immediate: true
          }
        },
       template: `
          <div class="tg-date-picker__widget">
            <vue-date-picker
              v-model="modelValue"
              :enable-time-picker="true"
              :is-24="true"
              :minute-increment="5"
              :action-row="actionRow"
              :teleport-center="true"
              locale="pl"
              input-class="tg-date-picker__input"
              :format="formatDisplay"
              :auto-apply="true"
              :clearable="true"
              @now="handleNow"
              @cleared="handleClear"
            />
          </div>
        `
      });

      app.mount(host);
    });
  }

  function init(){
    const metadata = parseChannelMetadata();
    initTextEditor(metadata);
    initDatePickers();
  }

  if (document.readyState === "loading"){
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
