(function(){
  function escapeHtml(value){
    return (value || "").replace(/[&<>\"]/g, function(ch){
      switch(ch){
        case "&": return "&amp;";
        case "<": return "&lt;";
        case ">": return "&gt;";
        case "\"": return "&quot;";
      }
      return ch;
    });
  }

  function escapeAttr(value){
    return (value || "").replace(/[&<>\"]/g, function(ch){
      switch(ch){
        case "&": return "&amp;";
        case "<": return "&lt;";
        case ">": return "&gt;";
        case "\"": return "&quot;";
      }
      return ch;
    });
  }

  function renderMediaItem(item){
    const type = item.type || "photo";
    const src = escapeAttr(item.src || "");
    if (!src){
      return "";
    }
    if (type === "video"){
      return `<video src="${src}" controls preload="metadata" playsinline muted loop></video>`;
    }
    if (type === "doc"){
      const name = escapeHtml(item.name || item.src || "Dokument");
      return `<div class="tg-doc"><span class="tg-doc__icon">📎</span><span class="tg-doc__name">${name}</span></div>`;
    }
    return `<img src="${src}" alt="">`;
  }

  function buildMediaHtml(media){
    if (!media || !media.length){
      return "";
    }
    if (media.length === 1){
      return `<div class="tg-media">${renderMediaItem(media[0])}</div>`;
    }
    const cols = media.length <= 4 ? 2 : (media.length <= 9 ? 3 : 4);
    const items = media.map(item => `<div class="tg-album__item">${renderMediaItem(item)}</div>`).join("");
    return `<div class="tg-album grid cols-${cols}">${items}</div>`;
  }

  document.addEventListener("DOMContentLoaded", function(){
    const IMAGE_EXT = new Set([".jpg",".jpeg",".png",".gif",".webp",".bmp",".tiff"]);
    const VIDEO_EXT = new Set([".mp4",".mov",".m4v",".avi",".mkv",".webm",".mpg",".mpeg"]);

    function normalizeExtension(value){
      return value ? value.toLowerCase().replace(/^.*(\.[a-z0-9]+)(?:\?.*)?$/, "$1") : "";
    }

    function guessMediaType(name, mime){
      const lowerMime = (mime || "").toLowerCase();
      const ext = normalizeExtension(name || "");
      if (lowerMime.startsWith("image/") || IMAGE_EXT.has(ext)){
        return "photo";
      }
      if (lowerMime.startsWith("video/") || VIDEO_EXT.has(ext)){
        return "video";
      }
      return "doc";
    }

    function applyGuessedType(form, hint){
      if (!form || !hint){
        return;
      }
      if (form.dataset.typeManuallySet === "1"){
        return;
      }
      const typeField = form.querySelector("[data-preview-type]");
      if (!typeField){
        return;
      }
      const current = typeField.value;
      if (current === hint){
        return;
      }
      typeField.value = hint;
      const evt = new Event("change", { bubbles: true });
      typeField.dispatchEvent(evt);
    }

    function guessFromUpload(form, input){
      if (!input){
        return;
      }
      const file = input.files && input.files[0];
      if (!file){
        return;
      }
      const guessed = guessMediaType(file.name, file.type);
      applyGuessedType(form, guessed);
    }

    function guessFromSource(form, input){
      if (!input){
        return;
      }
      const value = (input.value || input.getAttribute("data-existing-src") || "").trim();
      if (!value){
        return;
      }
      const guessed = guessMediaType(value, "");
      applyGuessedType(form, guessed);
    }

    const previewRoot = document.querySelector("[data-preview-card-root]");
    if (!previewRoot){
      return;
    }
    const previewBody = previewRoot.querySelector("[data-preview-body]");
    const previewChannel = previewRoot.querySelector("[data-preview-channel]");
    const previewStatus = previewRoot.querySelector("[data-preview-status-label]");
    const previewScheduleMode = previewRoot.querySelector("[data-preview-schedule-mode]");
    const previewScheduled = previewRoot.querySelector("[data-preview-scheduled]");

    const mediaNode = document.getElementById("post-preview-initial-media");
    let initialMedia = [];
    if (mediaNode){
      try {
        initialMedia = JSON.parse(mediaNode.textContent || "[]");
      } catch(err){
        initialMedia = [];
      }
    }

    const state = {
      text: "",
      channelName: previewChannel ? previewChannel.textContent.trim() : "",
      status: previewRoot.dataset.status || "DRAFT",
      statusLabel: previewStatus ? previewStatus.textContent.trim() : "",
      scheduleModeLabel: previewScheduleMode ? previewScheduleMode.textContent.trim() : "",
      scheduledDisplay: previewScheduled ? previewScheduled.textContent.trim() : "",
      media: initialMedia.slice()
    };

    function render(){
      if (previewChannel){
        previewChannel.textContent = state.channelName || "(wybierz kanał)";
      }
      if (previewStatus){
        previewStatus.textContent = state.statusLabel || state.status;
      }
      if (previewScheduleMode){
        previewScheduleMode.textContent = state.scheduleModeLabel || "";
      }
      if (previewScheduled){
        previewScheduled.textContent = state.scheduledDisplay || "–";
      }
      previewRoot.dataset.status = state.status || "DRAFT";
      const html = `<div class="tg-wrap" data-theme="dark" style="--w:100%">${buildMediaHtml(state.media)}<div class="tg-text">${escapeHtml(state.text || "")}</div></div>`;
      if (previewBody){
        previewBody.innerHTML = html;
      }
    }

    const textInput = document.getElementById("id_text");
    if (textInput){
      state.text = textInput.value || "";
      textInput.addEventListener("input", function(){
        state.text = this.value;
        render();
      });
    }

    const channelSelect = document.getElementById("id_channel");
    if (channelSelect){
      const updateChannel = function(){
        const option = channelSelect.options[channelSelect.selectedIndex];
        state.channelName = option ? option.text.trim() : "(wybierz kanał)";
        render();
      };
      updateChannel();
      channelSelect.addEventListener("change", updateChannel);
    }

    const statusSelect = document.getElementById("id_status");
    if (statusSelect){
      const updateStatus = function(){
        const option = statusSelect.options[statusSelect.selectedIndex];
        state.status = statusSelect.value || "DRAFT";
        state.statusLabel = option ? option.text.trim() : state.status;
        render();
      };
      updateStatus();
      statusSelect.addEventListener("change", updateStatus);
    }

    const scheduleModeSelect = document.getElementById("id_schedule_mode");
    if (scheduleModeSelect){
      const updateMode = function(){
        const option = scheduleModeSelect.options[scheduleModeSelect.selectedIndex];
        state.scheduleModeLabel = option ? option.text.trim() : scheduleModeSelect.value;
        render();
      };
      updateMode();
      scheduleModeSelect.addEventListener("change", updateMode);
    }

    const scheduleDate = document.getElementById("id_scheduled_at_0");
    const scheduleTime = document.getElementById("id_scheduled_at_1");
    const updateScheduled = function(){
      const dateVal = scheduleDate ? scheduleDate.value.trim() : "";
      const timeVal = scheduleTime ? scheduleTime.value.trim() : "";
      state.scheduledDisplay = (dateVal + " " + timeVal).trim();
      render();
    };
    if (scheduleDate){
      scheduleDate.addEventListener("input", updateScheduled);
    }
    if (scheduleTime){
      scheduleTime.addEventListener("input", updateScheduled);
    }
    updateScheduled();

    function ensurePreviewCell(form){
      let previewCell = form.querySelector('[data-post-media-preview]');
      if (previewCell){
        return previewCell;
      }
      previewCell = form.querySelector('.field-existing_file .readonly');
      if (previewCell){
        previewCell.setAttribute('data-post-media-preview', '1');
        return previewCell;
      }
      previewCell = document.createElement('div');
      previewCell.className = 'post-media-inline-preview';
      previewCell.setAttribute('data-post-media-preview', '1');
      previewCell.textContent = '—';
      const firstGroup = form.querySelector('.form-group, p');
      if (firstGroup && firstGroup.parentNode){
        firstGroup.parentNode.insertBefore(previewCell, firstGroup);
      } else {
        form.insertBefore(previewCell, form.firstChild);
      }
      return previewCell;
    }

    function updatePreviewCell(previewCell, html){
      if (!previewCell){
        return;
      }
      if (html){
        previewCell.innerHTML = html;
        previewCell.setAttribute('data-has-preview', '1');
      } else {
        previewCell.innerHTML = '—';
        previewCell.removeAttribute('data-has-preview');
      }
      previewCell.classList.remove('is-loading', 'has-error');
    }

    function setInlinePreview(form, item){
      const previewCell = ensurePreviewCell(form);
      if (!previewCell){
        return;
      }
      if (item && item.src){
        const mediaType = item.type || 'photo';
        if (mediaType === 'photo'){
          previewCell.classList.add('is-loading');
          updatePreviewCell(previewCell, '<span class="post-media-inline-spinner"></span>');
          const img = new Image();
          img.onload = function(){
            previewCell.classList.remove('is-loading');
            updatePreviewCell(previewCell, `<img src="${escapeAttr(item.src)}" alt="">`);
          };
          img.onerror = function(){
            previewCell.classList.remove('is-loading');
            previewCell.classList.add('has-error');
            updatePreviewCell(previewCell, '<span class="post-media-inline-error">Nie można załadować podglądu</span>');
          };
          img.src = item.src;
          return;
        }
        if (mediaType === 'video'){
          previewCell.classList.add('is-loading');
          updatePreviewCell(previewCell, '<span class="post-media-inline-spinner"></span>');
          const video = document.createElement('video');
          video.src = item.src;
          video.preload = 'metadata';
          video.muted = true;
          video.playsInline = true;
          video.onloadeddata = function(){
            previewCell.classList.remove('is-loading');
            updatePreviewCell(previewCell, renderMediaItem(item));
          };
          video.onerror = function(){
            previewCell.classList.remove('is-loading');
            previewCell.classList.add('has-error');
            updatePreviewCell(previewCell, '<span class="post-media-inline-error">Nie można załadować podglądu</span>');
          };
          video.load();
          return;
        }
        updatePreviewCell(previewCell, renderMediaItem(item));
      } else {
        updatePreviewCell(previewCell, null);
      }
    }

    function readInlineMedia(){
      if (!inlineGroup){
        return state.media.slice();
      }
      const collected = [];
      const forms = inlineGroup.querySelectorAll(".inline-related");
      forms.forEach(function(form){
        if (form.classList.contains("empty-form")){
          return;
        }
        const deleteField = form.querySelector('input[name$="-DELETE"]');
        if (deleteField && deleteField.checked){
          return;
        }
        const typeField = form.querySelector("[data-preview-type]");
        const orderField = form.querySelector("[data-preview-order]");
        const sourceField = form.querySelector("[data-preview-source]");
        const uploadField = form.querySelector("[data-preview-upload]");
        if (!typeField || !sourceField || !uploadField){
          return;
        }
        let src = "";
        let name = "";
        const files = uploadField.files;
        if (files && files[0]){
          if (form.dataset.previewObjectUrl){
            URL.revokeObjectURL(form.dataset.previewObjectUrl);
          }
          src = URL.createObjectURL(files[0]);
          form.dataset.previewObjectUrl = src;
          name = files[0].name;
        } else {
          if (form.dataset.previewObjectUrl){
            URL.revokeObjectURL(form.dataset.previewObjectUrl);
            delete form.dataset.previewObjectUrl;
          }
          src = sourceField.value.trim() || sourceField.getAttribute("data-existing-src") || "";
          name = sourceField.getAttribute("data-existing-name") || src.split("/").pop();
        }
        const mediaType = typeField && typeField.value ? typeField.value : "photo";
        const orderValue = parseFloat(orderField ? orderField.value : "0") || 0;
        if (!src){
          setInlinePreview(form, null);
          return;
        }
        const mediaItem = {
          src: src,
          type: mediaType,
          name: name,
          order: orderValue
        };
        setInlinePreview(form, mediaItem);
        collected.push(mediaItem);
      });
      collected.sort(function(a, b){ return a.order - b.order; });
      return collected;
    }

    function refreshMedia(){
      state.media = readInlineMedia();
      render();
    }

    const inlineGroup = document.querySelector('[data-inline-formset][id$="-group"]');
    if (inlineGroup){
      inlineGroup.addEventListener("change", function(event){
        const target = event.target;
        if (!target){
          return;
        }
        const form = target.closest(".inline-related");
        if (!form){
          return;
        }
        if (target.matches("[data-preview-type]")){
          if (event.isTrusted){
            form.dataset.typeManuallySet = "1";
          }
          return;
        }
        if (target.matches("[data-preview-upload]")){
          delete form.dataset.typeManuallySet;
          guessFromUpload(form, target);
        }
        if (target.matches("[data-preview-source]")){
          delete form.dataset.typeManuallySet;
          guessFromSource(form, target);
        }
      });

      inlineGroup.addEventListener("input", function(event){
        const target = event.target;
        if (!target){
          return;
        }
        const form = target.closest(".inline-related");
        if (!form){
          return;
        }
        if (target.matches("[data-preview-upload]")){
          guessFromUpload(form, target);
        }
        if (target.matches("[data-preview-source]")){
          guessFromSource(form, target);
        }
      });

      const handler = function(event){
        const target = event.target;
        if (!target){
          return;
        }
        if (target.matches("[data-preview-upload], [data-preview-source], [data-preview-type], [data-preview-order]") || target.name && target.name.endsWith("-DELETE")){
          refreshMedia();
        }
      };
      inlineGroup.addEventListener("input", handler);
      inlineGroup.addEventListener("change", handler);
      const addRow = inlineGroup.querySelector(".add-row a");
      if (addRow){
        addRow.addEventListener("click", function(){
          setTimeout(function(){
            refreshMedia();
            const newForm = inlineGroup.querySelector(".inline-related:last-child");
            if (newForm){
              const upload = newForm.querySelector("[data-preview-upload]");
              const source = newForm.querySelector("[data-preview-source]");
              guessFromUpload(newForm, upload);
              guessFromSource(newForm, source);
            }
          }, 50);
        });
      }
    }

    const bridge = {
      state,
      render,
      refreshMedia,
      readInlineMedia,
      setMedia(media){
        state.media = Array.isArray(media) ? media.slice() : [];
        render();
      }
    };
    window.postEditBridge = bridge;
    try {
      if (typeof CustomEvent === "function"){
        document.dispatchEvent(new CustomEvent("post-edit:ready", { detail: bridge }));
      } else if (document.createEvent){
        const evt = document.createEvent("CustomEvent");
        evt.initCustomEvent("post-edit:ready", false, false, bridge);
        document.dispatchEvent(evt);
      }
    } catch(err){
      // noop – event dispatch failure shouldn't block preview initialisation
    }

    render();
    refreshMedia();
  });
})();
