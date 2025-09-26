(function(){
  const POLISH_LOCALE = {
    weekdays: {
      shorthand: ["ndz", "pon", "wt", "śr", "czw", "pt", "sob"],
      longhand: [
        "niedziela",
        "poniedziałek",
        "wtorek",
        "środa",
        "czwartek",
        "piątek",
        "sobota"
      ]
    },
    months: {
      shorthand: [
        "sty",
        "lut",
        "mar",
        "kwi",
        "maj",
        "cze",
        "lip",
        "sie",
        "wrz",
        "paź",
        "lis",
        "gru"
      ],
      longhand: [
        "styczeń",
        "luty",
        "marzec",
        "kwiecień",
        "maj",
        "czerwiec",
        "lipiec",
        "sierpień",
        "wrzesień",
        "październik",
        "listopad",
        "grudzień"
      ]
    },
    firstDayOfWeek: 1,
    ordinal: () => "-ty",
    rangeSeparator: " do ",
    weekAbbreviation: "tydz.",
    scrollTitle: "Przewiń, aby zmienić",
    toggleTitle: "Kliknij, aby przełączyć",
    time_24hr: true
  };

  function ensureLocale(flatpickr){
    if (!flatpickr){
      return;
    }
    const locales = flatpickr.l10ns || {};
    if (!locales.pl){
      locales.pl = Object.assign({}, locales.default || {}, POLISH_LOCALE);
      flatpickr.l10ns = locales;
    } else {
      locales.pl = Object.assign({}, locales.pl, POLISH_LOCALE);
    }
  }

  function parseInitialValue(input){
    const value = (input.getAttribute("data-initial") || input.value || "").trim();
    if (!value){
      return null;
    }
    const parsed = value.replace("T", " ");
    const parts = parsed.split(/[\s]+/);
    if (parts.length < 2){
      return parsed;
    }
    return `${parts[0]} ${parts[1]}`.trim();
  }

  function pad(value){
    return String(value).padStart(2, "0");
  }

  function setHiddenValue(element, value){
    if (element){
      element.value = value || "";
    }
  }

  function dispatchUpdate(target){
    if (!target){
      return;
    }
    const inputEvent = new Event("input", { bubbles: true });
    const changeEvent = new Event("change", { bubbles: true });
    target.dispatchEvent(inputEvent);
    target.dispatchEvent(changeEvent);
    const customEvent = new Event("flatpickr:change", { bubbles: true });
    target.dispatchEvent(customEvent);
  }

  function initField(input){
    if (!window.flatpickr || input._flatpickrInstance){
      return;
    }
    ensureLocale(window.flatpickr);
    const dateInputId = input.getAttribute("data-flatpickr-date-input");
    const timeInputId = input.getAttribute("data-flatpickr-time-input");
    const dateInput = dateInputId ? document.getElementById(dateInputId) : null;
    const timeInput = timeInputId ? document.getElementById(timeInputId) : null;

    const initial = parseInitialValue(input);
    const options = {
      enableTime: true,
      time_24hr: true,
      dateFormat: "Y-m-d H:i",
      allowInput: true,
      locale: "pl",
      defaultDate: initial || undefined,
      onValueUpdate: function(selectedDates){
        if (!selectedDates || !selectedDates.length){
          setHiddenValue(dateInput, "");
          setHiddenValue(timeInput, "");
          dispatchUpdate(input);
          return;
        }
        const date = selectedDates[0];
        setHiddenValue(dateInput, `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`);
        setHiddenValue(timeInput, `${pad(date.getHours())}:${pad(date.getMinutes())}`);
        input.value = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
        dispatchUpdate(input);
      },
      onOpen: function(){
        input.classList.add("is-open");
      },
      onClose: function(selectedDates){
        input.classList.remove("is-open");
        if (!selectedDates || !selectedDates.length){
          dispatchUpdate(input);
        }
      },
      plugins: []
    };

    const instance = window.flatpickr(input, options);
    input._flatpickrInstance = instance;

    input.addEventListener("change", function(){
      const rawValue = (input.value || "").trim();
      if (!rawValue){
        setHiddenValue(dateInput, "");
        setHiddenValue(timeInput, "");
        dispatchUpdate(input);
        return;
      }
      instance.setDate(rawValue, false);
    });

    if (!instance.selectedDates.length){
      const storedDate = (dateInput && dateInput.value) ? dateInput.value.trim() : "";
      const storedTime = (timeInput && timeInput.value) ? timeInput.value.trim() : "";
      const combined = (storedDate + " " + storedTime).trim();
      if (combined){
        instance.setDate(combined, false);
      }
    }
  }

  function initAll(context){
    const scope = context || document;
    const inputs = scope.querySelectorAll(".js-datetime-picker");
    inputs.forEach(function(input){
      initField(input);
    });
  }

  function onReady(){
    initAll(document);
  }

  if (document.readyState === "loading"){
    document.addEventListener("DOMContentLoaded", onReady);
  } else {
    onReady();
  }

  document.addEventListener("formset:added", function(event){
    if (event && event.target){
      initAll(event.target);
    }
  });
})();
