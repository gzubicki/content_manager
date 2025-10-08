(function(){
  "use strict";

  function countErrors(){
    const lists = Array.from(document.querySelectorAll("ul.errorlist"));
    return lists.reduce((total, list) => total + list.querySelectorAll("li").length, 0);
  }

  function ensureErrorNote(){
    if (document.querySelector(".errornote")){
      return;
    }
    const errorCount = countErrors();
    if (!errorCount){
      return;
    }
    const container = document.querySelector("#content-main") || document.querySelector("#content");
    if (!container){
      return;
    }
    const note = document.createElement("p");
    note.className = "errornote";
    note.textContent = errorCount === 1 ? "Popraw zaznaczony błąd." : "Popraw zaznaczone błędy.";
    container.insertAdjacentElement("afterbegin", note);
  }

  if (document.readyState === "loading"){
    document.addEventListener("DOMContentLoaded", ensureErrorNote, { once: true });
  } else {
    ensureErrorNote();
  }
})();
