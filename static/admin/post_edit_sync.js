(function(){
  const POLL_INTERVAL = 5000;
  const RETRY_INTERVAL = 15000;
  const INITIAL_DELAY = 1500;

  const dirtyFields = new Map();
  let pollTimer = null;
  let isFetching = false;
  let statusEndpoint = "";
  let previewBridge = window.postEditBridge || null;
  let noticeRoot = null;
  let rewriteInitialized = false;
  let lastCompletionChecksum = "";

  function fieldKey(field){
    if (!field){
      return null;
    }
    if (field.name){
      return field.name;
    }
    if (field.id){
      return field.id;
    }
    return null;
  }

  function currentValue(field){
    if (!field){
      return "";
    }
    const tag = field.tagName;
    const type = (field.type || "").toLowerCase();
    if (type === "checkbox"){
      return field.checked ? "1" : "0";
    }
    if (field.multiple){
      const values = Array.from(field.selectedOptions || []).map(option => option.value);
      return JSON.stringify(values);
    }
    if (type === "file"){
      return field.files && field.files.length ? "__FILE__" : "";
    }
    return field.value != null ? String(field.value) : "";
  }

  function normalizeRemoteValue(field, value){
    if (!field){
      return "";
    }
    const type = (field.type || "").toLowerCase();
    if (type === "checkbox"){
      return value ? "1" : "0";
    }
    if (field.multiple){
      const values = Array.isArray(value) ? value : (value == null ? [] : [value]);
      return JSON.stringify(values.map(String));
    }
    if (value == null){
      return "";
    }
    return String(value);
  }

  function applyNormalizedValue(field, normalized){
    if (!field){
      return;
    }
    const type = (field.type || "").toLowerCase();
    if (type === "checkbox"){
      field.checked = normalized === "1";
      return;
    }
    if (field.multiple){
      let values = [];
      try {
        values = JSON.parse(normalized || "[]");
      } catch(err){
        values = [];
      }
      const expected = new Set((values || []).map(String));
      Array.from(field.options || []).forEach(option => {
        option.selected = expected.has(option.value);
      });
      return;
    }
    if (type === "file"){
      return;
    }
    field.value = normalized;
  }

  function triggerFieldUpdate(field){
    if (!field){
      return;
    }
    const tag = field.tagName;
    const type = (field.type || "").toLowerCase();
    if (tag === "TEXTAREA" || (tag === "INPUT" && ["text","search","url","email","tel","number","date","time","datetime-local"].includes(type))){
      field.dispatchEvent(new Event("input", { bubbles: true }));
    }
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function clearDirty(field){
    const key = fieldKey(field);
    if (!key){
      return;
    }
    dirtyFields.delete(key);
    if (field && field.dataset){
      delete field.dataset.localDirty;
    }
  }

  function syncField(field, remoteValue){
    if (!field){
      return false;
    }
    const key = fieldKey(field);
    const normalizedRemote = normalizeRemoteValue(field, remoteValue);
    if (key && dirtyFields.has(key)){
      const current = currentValue(field);
      dirtyFields.set(key, current);
      if (current !== normalizedRemote){
        return false;
      }
      clearDirty(field);
    }
    const current = currentValue(field);
    if (current === normalizedRemote){
      return false;
    }
    applyNormalizedValue(field, normalizedRemote);
    triggerFieldUpdate(field);
    clearDirty(field);
    return true;
  }

  function markDirty(field){
    const key = fieldKey(field);
    if (!key){
      return;
    }
    dirtyFields.set(key, currentValue(field));
    if (field && field.dataset){
      field.dataset.localDirty = "1";
    }
  }

  function handleDirtyEvent(event){
    if (!event || !event.isTrusted){
      return;
    }
    const target = event.target;
    if (!target || target.type === "hidden"){
      return;
    }
    markDirty(target);
  }

  function isFieldDirty(field){
    const key = fieldKey(field);
    return Boolean(key && dirtyFields.has(key));
  }

  function isFormDirty(form){
    if (!form){
      return false;
    }
    const fields = form.querySelectorAll("input[name], select[name], textarea[name]");
    for (const field of fields){
      if (isFieldDirty(field)){
        return true;
      }
    }
    return false;
  }

  function getInlineGroup(){
    return document.getElementById("postmedia_set-group");
  }

  function getInlineForms(inlineGroup){
    if (!inlineGroup){
      return [];
    }
    return Array.from(inlineGroup.querySelectorAll(".inline-related")).filter(form => !form.classList.contains("empty-form"));
  }

  function formObjectId(form){
    if (!form){
      return "";
    }
    const idField = form.querySelector('input[name$="-id"]');
    if (!idField){
      return "";
    }
    return (idField.value || "").trim();
  }

  function dispatchFormsetAdded(form, inlineGroup, context){
    if (!form){
      return;
    }
    let prefix = "";
    const ctx = context && context.options ? context.options : null;
    if (ctx && typeof ctx.prefix === "string"){
      prefix = ctx.prefix;
    } else if (ctx && typeof ctx.name === "string"){
      prefix = ctx.name;
    } else if (inlineGroup){
      const totalInput = inlineGroup.querySelector('input[name$="-TOTAL_FORMS"]');
      if (totalInput){
        prefix = totalInput.name.replace(/-TOTAL_FORMS$/, "");
      }
    }
    if (window.django && window.django.jQuery){
      try {
        window.django.jQuery(form).trigger("formset:added", [form, prefix]);
      } catch(err){
        // best effort only
      }
    }
    try {
      form.dispatchEvent(new CustomEvent("formset:added", {
        bubbles: true,
        detail: {
          formsetName: prefix,
        },
      }));
    } catch(err){
      // ignore
    }
  }

  function cloneInlineFromTemplate(inlineGroup){
    if (!inlineGroup){
      return null;
    }
    const emptyForm = inlineGroup.querySelector(".inline-related.empty-form");
    const totalInput = inlineGroup.querySelector('input[name$="-TOTAL_FORMS"]');
    if (!emptyForm || !totalInput){
      return null;
    }
    const prefix = (totalInput.name || "").replace(/-TOTAL_FORMS$/, "");
    const currentIndex = parseInt(totalInput.value || "0", 10) || 0;

    const newForm = emptyForm.cloneNode(true);
    newForm.classList.remove("empty-form", "last-related", "post-media-inline--remote-removed");
    newForm.style.display = "";

    const patternPrefix = prefix ? new RegExp(prefix + "-__prefix__", "g") : null;
    const patternGeneric = /__prefix__/g;

    (function walk(node){
      if (node.nodeType === Node.ELEMENT_NODE){
        Array.from(node.attributes).forEach(attr => {
          if (attr.value && attr.value.indexOf("__prefix__") !== -1){
            let updated = attr.value;
            if (patternPrefix){
              updated = updated.replace(patternPrefix, prefix + "-" + currentIndex);
            }
            updated = updated.replace(patternGeneric, currentIndex);
            node.setAttribute(attr.name, updated);
          }
        });
      } else if (node.nodeType === Node.TEXT_NODE){
        if (node.textContent && node.textContent.indexOf("__prefix__") !== -1){
          node.textContent = node.textContent.replace(patternGeneric, currentIndex);
        }
      }
      Array.from(node.childNodes || []).forEach(walk);
    })(newForm);

    totalInput.value = String(currentIndex + 1);
    emptyForm.parentNode.insertBefore(newForm, emptyForm);

    const inlineContext = inlineGroup.dataset && inlineGroup.dataset.inlineFormset
      ? (function(){
          try {
            return JSON.parse(inlineGroup.dataset.inlineFormset);
          } catch(err){
            return null;
          }
        })()
      : null;

    dispatchFormsetAdded(newForm, inlineGroup, { options: inlineContext });
    return newForm;
  }

  function ensureInlineForm(inlineGroup){
    if (!inlineGroup){
      return null;
    }
    const before = getInlineForms(inlineGroup);
    const addLink = inlineGroup.querySelector(".add-row a");
    if (addLink){
      let clickHandled = false;
      if (window.django && window.django.jQuery){
        try {
          window.django.jQuery(addLink).trigger("click");
          clickHandled = true;
        } catch(err){
          clickHandled = false;
        }
      }
      if (!clickHandled){
        const synthetic = new MouseEvent("click", { bubbles: true, cancelable: true });
        addLink.dispatchEvent(synthetic);
        if (!synthetic.defaultPrevented){
          try {
            addLink.click();
          } catch(err){
            // ignore
          }
        }
      }
      const formsAfter = getInlineForms(inlineGroup);
      if (formsAfter.length > before.length){
        const created = formsAfter[formsAfter.length - 1];
        created.classList.add("has_original");
        created.classList.remove("dynamic-postmedia", "post-media-inline--remote-removed");
        created.style.display = "";
        return created;
      }
    }
    const fallback = cloneInlineFromTemplate(inlineGroup);
    if (fallback){
      fallback.classList.add("has_original");
      fallback.classList.remove("dynamic-postmedia", "post-media-inline--remote-removed");
      fallback.style.display = "";
    }
    return fallback;
  }

  function updateMediaForm(form, item){
    if (!form || !item){
      return;
    }
    form.dataset.remoteId = String(item.id);
    form.style.display = "";
    form.classList.remove("post-media-inline--remote-removed");

    const orderField = form.querySelector('[data-preview-order]');
    syncField(orderField, item.order);

    const typeField = form.querySelector('[data-preview-type]');
    syncField(typeField, item.type || "photo");

    const spoilerField = form.querySelector('[data-preview-spoiler]');
    syncField(spoilerField, Boolean(item.has_spoiler));

    const deleteField = form.querySelector('input[name$="-DELETE"]');
    syncField(deleteField, false);

    const sourceField = form.querySelector('[data-preview-source]');
    if (sourceField){
      syncField(sourceField, item.source_url || "");
      if (item.media_public_url){
        sourceField.setAttribute("data-existing-src", item.media_public_url);
      } else {
        sourceField.removeAttribute("data-existing-src");
      }
      if (item.name){
        sourceField.setAttribute("data-existing-name", item.name);
      } else {
        sourceField.removeAttribute("data-existing-name");
      }
    }

    const uploadField = form.querySelector('[data-preview-upload]');
    if (uploadField && !(uploadField.files && uploadField.files.length) && form.dataset.previewObjectUrl){
      try {
        URL.revokeObjectURL(form.dataset.previewObjectUrl);
      } catch(err){
        // ignore
      }
      delete form.dataset.previewObjectUrl;
    }
  }

  function syncMediaList(mediaItems){
    const inlineGroup = getInlineGroup();
    if (!inlineGroup){
      return;
    }
    const forms = getInlineForms(inlineGroup);
    const formById = new Map();
    forms.forEach(form => {
      const identifier = formObjectId(form);
      if (identifier){
        formById.set(identifier, form);
      }
    });

    const orderedMedia = Array.isArray(mediaItems) ? mediaItems.slice() : [];
    orderedMedia.sort((a, b) => {
      const orderA = Number(a && a.order != null ? a.order : 0);
      const orderB = Number(b && b.order != null ? b.order : 0);
      if (orderA !== orderB){
        return orderA - orderB;
      }
      const idA = Number(a && a.id != null ? a.id : 0);
      const idB = Number(b && b.id != null ? b.id : 0);
      return idA - idB;
    });

    const seen = new Set();

    orderedMedia.forEach(item => {
      const itemId = item && item.id != null ? String(item.id) : "";
      if (!itemId){
        return;
      }
      let form = formById.get(itemId);
      if (!form){
        form = ensureInlineForm(inlineGroup);
        if (!form){
          return;
        }
        const idField = form.querySelector('input[name$="-id"]');
        if (idField){
          idField.value = itemId;
        }
        formById.set(itemId, form);
      }
      seen.add(form);
      updateMediaForm(form, item);
    });

    forms.forEach(form => {
      if (seen.has(form)){
        return;
      }
      const identifier = formObjectId(form);
      if (!identifier){
        return;
      }
      if (isFormDirty(form)){
        return;
      }
      const deleteField = form.querySelector('input[name$="-DELETE"]');
      syncField(deleteField, true);
      form.classList.add("post-media-inline--remote-removed");
      form.style.display = "none";
    });
  }

  function syncPostFields(post){
    if (!post){
      return;
    }
    syncField(document.getElementById("id_text"), post.text || "");
    syncField(document.getElementById("id_status"), post.status || "");
    syncField(document.getElementById("id_schedule_mode"), post.schedule_mode || "");
    syncField(document.getElementById("id_channel"), post.channel_id ? String(post.channel_id) : "");
    syncField(document.getElementById("id_scheduled_at_0"), post.scheduled_date || "");
    syncField(document.getElementById("id_scheduled_at_1"), post.scheduled_time || "");
  }

  function buildRewriteText(state){
    const status = state && typeof state === "object" ? (state.status || "") : "";
    if (status === "pending"){
      const when = state.requested_display || "przed chwilą";
      return `⏳ Korekta GPT w toku — zlecono ${when}.`;
    }
    if (status === "completed"){
      let text = "✅ Wpis wrócił z korekty GPT";
      if (state.completed_display){
        text += ` (${state.completed_display})`;
      }
      return `${text}.`;
    }
    return "";
  }

  function renderRewriteNotice(state, highlight){
    if (!noticeRoot){
      return;
    }
    noticeRoot.innerHTML = "";
    const text = buildRewriteText(state);
    if (!text){
      noticeRoot.dataset.rewriteStatus = "";
      return;
    }
    const status = state.status || "";
    const notice = document.createElement("div");
    notice.className = "post-edit__notice";
    if (status === "pending"){
      notice.classList.add("post-edit__notice--pending");
    } else if (status === "completed"){
      notice.classList.add("post-edit__notice--done");
    }
    notice.textContent = text;
    if (highlight){
      notice.classList.add("post-edit__notice--highlight");
      setTimeout(() => {
        notice.classList.remove("post-edit__notice--highlight");
      }, 4000);
    }
    noticeRoot.appendChild(notice);
    noticeRoot.dataset.rewriteStatus = status;
  }

  function handleRewriteState(state){
    if (!state || typeof state !== "object"){
      renderRewriteNotice(null, false);
      if (rewriteInitialized){
        lastCompletionChecksum = "";
      }
      return;
    }
    const checksum = state.text_checksum || state.textChecksum || "";
    const status = state.status || "";
    let highlight = false;
    if (status === "completed" && checksum){
      if (rewriteInitialized && checksum !== lastCompletionChecksum){
        highlight = true;
      }
      lastCompletionChecksum = checksum;
    }
    renderRewriteNotice(state, highlight);
    rewriteInitialized = true;
  }

  function applyPayload(payload){
    if (!payload || typeof payload !== "object"){
      return;
    }
    try {
      syncPostFields(payload.post || null);
      syncMediaList(payload.media || []);
      handleRewriteState(payload.rewrite || null);
      const bridge = previewBridge || window.postEditBridge;
      if (bridge && typeof bridge.refreshMedia === "function"){
        bridge.refreshMedia();
      }
    } catch(err){
      console.warn("Nie udało się zsynchronizować stanu posta", err);
    }
  }

  function scheduleNext(delay){
    if (pollTimer){
      clearTimeout(pollTimer);
    }
    pollTimer = setTimeout(runPoll, delay);
  }

  async function runPoll(){
    if (!statusEndpoint || isFetching){
      return;
    }
    if (document.hidden){
      scheduleNext(POLL_INTERVAL);
      return;
    }
    isFetching = true;
    let nextDelay = POLL_INTERVAL;
    try {
      const response = await fetch(statusEndpoint, {
        method: "GET",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
        },
      });
      if (response.status === 404){
        statusEndpoint = "";
        return;
      }
      if (!response.ok){
        nextDelay = RETRY_INTERVAL;
      } else {
        const data = await response.json();
        applyPayload(data);
      }
    } catch(err){
      nextDelay = RETRY_INTERVAL;
    } finally {
      isFetching = false;
      if (statusEndpoint){
        scheduleNext(nextDelay);
      }
    }
  }

  document.addEventListener("visibilitychange", function(){
    if (!document.hidden && statusEndpoint){
      scheduleNext(0);
    }
  });

  document.addEventListener("DOMContentLoaded", function(){
    const root = document.querySelector(".post-edit");
    if (!root){
      return;
    }
    noticeRoot = root.querySelector("[data-post-edit-notices]");

    const stateNode = document.getElementById("post-rewrite-state");
    if (stateNode){
      try {
        const initialState = JSON.parse(stateNode.textContent || "{}") || {};
        handleRewriteState(initialState);
      } catch(err){
        handleRewriteState(null);
      }
    }

    const url = (root.getAttribute("data-status-url") || "").trim();
    if (url){
      statusEndpoint = url;
    }

    const form = root.querySelector("form");
    if (form){
      form.addEventListener("input", handleDirtyEvent, true);
      form.addEventListener("change", handleDirtyEvent, true);
    }

    document.addEventListener("post-edit:ready", function(event){
      previewBridge = event && event.detail ? event.detail : window.postEditBridge;
    });

    if (statusEndpoint){
      scheduleNext(INITIAL_DELAY);
    }
  });
})();
