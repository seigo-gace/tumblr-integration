"use strict";
// Presentation-only controller: external operations are disabled until verified adapters exist.
// The nine names are the repository's adopted module catalogue, not a replacement for
// the six objective shortcuts whose final labels must be checked against the UI decision record.
const catalog = Object.freeze([
  ["account","アカウント","Tumblr OAuth接続とアカウント情報"],
  ["discovery","検索・発見","Tumblr投稿・ブログの検索"],
  ["library","ライブラリ","保存・お気に入り・後で見る"],
  ["media","メディア","画像と動画の管理"],
  ["engagement","交流","反応や交流の管理"],
  ["publishing","投稿","下書き・公開・編集"],
  ["campaigns","キャンペーン","集客とキャンペーン"],
  ["referrals","紹介","紹介・参加資格連携"],
  ["analytics","分析","閲覧・反応の集計"]
]);
(function init(){
  const nav=document.getElementById("modules");
  const title=document.getElementById("selected-title");
  const description=document.getElementById("selected-description");
  const messages=document.getElementById("messages");
  const form=document.getElementById("chat-form");
  const instruction=document.getElementById("instruction");
  let current=null;
  function display(text,role){
    const item=document.createElement("div");
    item.className="message "+role;
    // Never render user input as HTML.
    item.textContent=text;
    messages.appendChild(item);
    messages.scrollTop=messages.scrollHeight;
  }
  function selectModule(item){
    current=item;
    title.textContent=item[1];
    description.textContent=item[2];
    nav.querySelectorAll(".module").forEach(button=>{
      const on=button.dataset.module===item[0];
      button.classList.toggle("active",on);
      button.setAttribute("aria-pressed",String(on));
    });
    display("「"+item[1]+"」を選択しました。現在は外部接続前の操作入口です。実操作は行われません。","system");
    instruction.focus();
  }
  catalog.forEach(item=>{
    const button=document.createElement("button");
    button.type="button";
    button.className="module";
    button.dataset.module=item[0];
    button.setAttribute("aria-pressed","false");
    const name=document.createElement("strong");
    name.textContent=item[1];
    const badge=document.createElement("span");
    badge.textContent="未接続";
    button.append(name,badge);
    button.addEventListener("click",()=>selectModule(item));
    nav.appendChild(button);
  });
  form.addEventListener("submit",event=>{
    event.preventDefault();
    const message=instruction.value.trim();
    if(!message)return;
    if(!current){
      display("操作分野を選んでください。","system");
      return;
    }
    display(message,"user");
    instruction.value="";
    display("「"+current[1]+"」への指示を受け付けましたが、現在この画面は未接続です。外部送信・投稿・アカウント変更は実行していません。接続後に実行できるようになります。","system");
  });
  instruction.addEventListener("keydown",event=>{
    if(event.key==="Enter"&&(event.ctrlKey||event.metaKey)){
      event.preventDefault();
      form.requestSubmit();
    }
  });
  selectModule(catalog[0]);
})();