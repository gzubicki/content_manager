(function(){
  const doc = document;

  function onReady(fn){
    if (doc.readyState === "loading"){
      doc.addEventListener("DOMContentLoaded", fn, { once: true });
    } else {
      fn();
    }
  }

  function isMobileViewport(){
    return window.matchMedia && window.matchMedia("(max-width: 767px)").matches;
  }

  function initEditors(root){
    if (typeof window.Quill !== "function"){
      return;
    }
    const surfaces = root.querySelectorAll("[data-admin-editor]");
    surfaces.forEach((surface) => {
      if (surface.dataset.editorBound === "1"){
        return;
      }
      const inputSelector = surface.getAttribute("data-editor-input");
      if (!inputSelector){
        return;
      }
      const input = root.querySelector(inputSelector);
      if (!input){
        return;
      }
      const wrapper = surface.closest("[data-editor-container]");
      const mode = surface.getAttribute("data-editor-mode") || "post";
      const placeholder = surface.getAttribute("data-editor-placeholder") || input.getAttribute("placeholder") || "";
      const mobile = isMobileViewport();
      let toolbar;
      const toolbarAttr = surface.getAttribute("data-editor-toolbar");
      if (toolbarAttr){
        try {
          toolbar = JSON.parse(toolbarAttr);
        } catch (err){
          toolbar = null;
        }
      }
      if (!toolbar){
        const toolbars = {
          post: mobile
            ? [["bold", "italic", "underline"], [{ list: "ordered" }, { list: "bullet" }], ["clean"]]
            : [[{ header: [2, 3, false] }], ["bold", "italic", "underline"], [{ list: "ordered" }, { list: "bullet" }], ["clean"]],
          prompt: mobile ? [["bold", "italic"], ["clean"]] : [["bold", "italic", "underline"], ["clean"]],
        };
        toolbar = toolbars[mode] || toolbars.post;
      }
      const quill = new window.Quill(surface, {
        theme: "snow",
        placeholder,
        modules: { toolbar },
      });
      surface.__adminEditor = quill;
      surface.dataset.editorBound = "1";
      const counter = wrapper ? wrapper.querySelector("[data-admin-editor-count]") : null;
      const maxLengthAttr = input.getAttribute("maxlength") || surface.getAttribute("data-editor-maxlength");
      const maxLength = maxLengthAttr ? parseInt(maxLengthAttr, 10) || 0 : 0;
      const normalize = (text) => (text || "").replace(/\r\n/g, "\n");
      const trimTrailing = (text) => text.replace(/[\s\u00a0]+$/g, "");
      let internalUpdate = false;

      const updateCounter = (length) => {
        if (wrapper){
          wrapper.setAttribute("data-editor-length", String(length));
        }
        if (!counter){
          return;
        }
        const value = maxLength > 0 ? `${length} / ${maxLength}` : String(length);
        counter.textContent = value;
        counter.dataset.length = String(length);
        if (maxLength > 0){
          counter.dataset.maxLength = String(maxLength);
          if (wrapper){
            wrapper.setAttribute("data-editor-progress", Math.min(1, length / maxLength).toFixed(2));
          }
        }
      };

      const syncInput = (dispatch = true) => {
        if (!input){
          return;
        }
        const raw = trimTrailing(normalize(quill.getText() || ""));
        if (input.value !== raw){
          internalUpdate = true;
          input.value = raw;
        }
        updateCounter(raw.length);
        if (dispatch){
          const event = new Event("input", { bubbles: true });
          input.dispatchEvent(event);
        }
        if (internalUpdate){
          window.requestAnimationFrame(() => {
            internalUpdate = false;
          });
        }
      };

      quill.on("text-change", () => {
        syncInput(true);
      });

      input.addEventListener("input", () => {
        if (internalUpdate){
          return;
        }
        const normalized = trimTrailing(normalize(input.value || ""));
        internalUpdate = true;
        quill.setText(normalized, "silent");
        updateCounter(normalized.length);
        window.requestAnimationFrame(() => {
          internalUpdate = false;
        });
      });

      const initial = trimTrailing(normalize(input.value || ""));
      quill.setText(initial, "silent");
      updateCounter(initial.length);
      syncInput(false);

      if (input.form){
        input.form.addEventListener("submit", () => syncInput(false));
      }
      if (wrapper){
        wrapper.setAttribute("data-editor-ready", "1");
        wrapper.dataset.editorMode = mode;
        if (input.disabled || input.readOnly){
          wrapper.setAttribute("data-editor-disabled", "1");
          quill.enable(false);
        }
      }
    });
  }

  function initDatepickers(root){
    if (typeof window.flatpickr !== "function"){
      return;
    }
    const mobile = isMobileViewport();
    const inputs = root.querySelectorAll("[data-admin-datetime-input]");
    inputs.forEach((input) => {
      if (input.dataset.datetimeBound === "1"){
        return;
      }
      const container = input.closest("[data-admin-datetime]");
      const dateFormat = input.getAttribute("data-date-format") || "Y-m-d H:i";
      const altFormat = input.getAttribute("data-alt-format") || "d.m.Y H:i";
      const localeName = input.getAttribute("data-locale") || "pl";
      const minuteAttr = input.getAttribute("data-minute-step");
      const minuteIncrement = minuteAttr ? parseInt(minuteAttr, 10) || 5 : 5;
      const locale = window.flatpickr.l10ns && window.flatpickr.l10ns[localeName] ? window.flatpickr.l10ns[localeName] : undefined;

      const syncDisplay = (instance) => {
        const date = instance.selectedDates && instance.selectedDates[0] ? instance.selectedDates[0] : null;
        const displayValue = date ? instance.formatDate(date, altFormat) : "";
        input.dataset.displayValue = displayValue;
        if (container){
          container.setAttribute("data-datetime-ready", "1");
        }
        const event = new Event("input", { bubbles: true });
        input.dispatchEvent(event);
      };

      input.dataset.datetimeBound = "1";
      const picker = window.flatpickr(input, {
        enableTime: true,
        time_24hr: true,
        allowInput: true,
        altInput: true,
        altFormat,
        dateFormat,
        minuteIncrement,
        defaultDate: input.value || undefined,
        locale,
        disableMobile: mobile ? false : true,
        onReady(selectedDates, value, instance){
          if (container){
            container.setAttribute("data-datetime-ready", "1");
          }
          if (instance.altInput){
            instance.altInput.classList.add("admin-datetime__display");
            const altPlaceholder = input.getAttribute("data-alt-placeholder");
            if (altPlaceholder){
              instance.altInput.setAttribute("placeholder", altPlaceholder);
            }
          }
          syncDisplay(instance);
        },
        onValueUpdate(selectedDates, value, instance){
          syncDisplay(instance);
        },
        onClose(selectedDates, value, instance){
          syncDisplay(instance);
        },
      });

      const clearButton = container ? container.querySelector("[data-datetime-clear]") : null;
      if (clearButton){
        clearButton.addEventListener("click", (event) => {
          event.preventDefault();
          picker.clear();
          input.value = "";
          input.removeAttribute("data-display-value");
          syncDisplay(picker);
        });
      }
    });
  }

  function boot(){
    const root = doc;
    initEditors(root);
    initDatepickers(root);
  }

  onReady(boot);
})();
