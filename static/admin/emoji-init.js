function insertAtCursor(el, text){
  const s = el.selectionStart, e = el.selectionEnd;
  el.value = el.value.slice(0, s) + text + el.value.slice(e);
  el.focus();
  el.selectionStart = el.selectionEnd = s + text.length;
}
document.addEventListener("click", (ev)=>{
  if(ev.target.classList.contains("emoji-toggle")){
    const btn = ev.target, picker = btn.parentElement.querySelector("emoji-picker");
    picker.classList.toggle("hidden");
  }
});
customElements.whenDefined('emoji-picker').then(()=>{
  document.querySelectorAll('emoji-picker').forEach(p=>{
    p.addEventListener('emoji-click', (e)=>{
      const ta = document.getElementById("post-text");
      insertAtCursor(ta, e.detail.unicode);
    });
  });
});
