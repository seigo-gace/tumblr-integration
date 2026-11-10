"use strict";
// Assistant-first screen. A module button is not required before sending.
// Same-origin /assistant only. Replies are text, never HTML.
(function init(){
  const messages=document.getElementById("messages");
  const form=document.getElementById("chat-form");
  const instruction=document.getElementById("instruction");
  const connection=document.getElementById("connection");
  const assist=document.getElementById("assist");
  const confirm=document.getElementById("confirm");
  let lastText="";
  let pending=null;
  function display(text,role){
    const item=document.createElement("div");
    item.className="message "+role;
    item.textContent=text;
    messages.appendChild(item);
    messages.scrollTop=messages.scrollHeight;
  }
  function showAssist(turn){
    const open=Boolean(turn&&turn.recovery);
    assist.hidden=!open;
    confirm.hidden=!(turn&&turn.state==="confirmation_required"&&turn.request_id);
  }
  function apply(turn){
    display(turn.text||"応答を表示できません。実行していません。","system");
    connection.textContent=turn.executed?"外部操作：結果を確認":"外部接続：未実行";
    pending=turn.request_id||null;
    showAssist(turn);
  }
  function fail(){
    apply({text:"画面から操作先へ届きませんでした。実行していません。",executed:false,recovery:true,state:"failed"});
  }
  function post(payload){
    return fetch("/assistant",{
      method:"POST",
      credentials:"same-origin",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload)
    }).then(function(response){
      if(!response.ok) throw new Error("http");
      return response.json();
    }).then(apply,fail);
  }
  form.addEventListener("submit",function(event){
    event.preventDefault();
    const message=instruction.value.trim();
    if(!message) return;
    lastText=message;
    display(message,"user");
    instruction.value="";
    post({message:message});
  });
  document.getElementById("retry").addEventListener("click",function(){
    if(!lastText) return;
    post({message:lastText});
  });
  document.getElementById("draft").addEventListener("click",function(){
    post({draft:true,message:lastText});
  });
  document.getElementById("abort").addEventListener("click",function(){
    post({abort:true});
  });
  confirm.addEventListener("click",function(){
    if(!pending) return;
    post({confirm:true,request_id:pending});
  });
  // Real app-owned drafts: server-side SQLite, not browser storage or AI.
  const draftList=document.getElementById("draft-list");
  const saveDraft=document.getElementById("save-draft");
  const showDrafts=document.getElementById("show-drafts");
  let draftBusy=false;
  function draftNotice(text){
    draftList.replaceChildren();
    const item=document.createElement("p");
    item.textContent=text;
    draftList.appendChild(item);
  }
  function draftRequest(path,method,payload){
    const options={method:method,credentials:"same-origin",headers:{"Content-Type":"application/json"}};
    if(payload) options.body=JSON.stringify(payload);
    return fetch(path,options).then(function(res){
      if(!res.ok) throw new Error("draft_http_"+res.status);
      return res.json();
    });
  }
  function refreshDrafts(){
    if(draftBusy) return Promise.resolve();
    draftBusy=true;
    showDrafts.disabled=true;
    return draftRequest("/drafts","GET").then(function(data){
      draftList.replaceChildren();
      const drafts=Array.isArray(data.drafts)?data.drafts:[];
      if(!drafts.length){draftNotice("保存した下書きはありません。");return;}
      drafts.forEach(function(draft){
        const item=document.createElement("div");
        item.className="draft-item";
        const text=document.createElement("p");
        text.textContent=draft.text;
        const controls=document.createElement("div");
        controls.className="draft-actions";
        const load=document.createElement("button");
        load.type="button";
        load.textContent="編集する";
        load.addEventListener("click",function(){
          instruction.value=draft.text;
          instruction.focus();
          display("下書きを入力欄に読み込みました。外部には送信していません。","system");
        });
        const remove=document.createElement("button");
        remove.type="button";
        remove.textContent="削除";
        remove.addEventListener("click",function(){
          if(!window.confirm("この下書きを削除しますか？")) return;
          draftRequest("/drafts/"+encodeURIComponent(draft.id),"DELETE").then(refreshDrafts,function(){
            draftNotice("削除に失敗しました。下書きは保持されています。");
          });
        });
        controls.append(load,remove);
        item.append(text,controls);
        draftList.appendChild(item);
      });
    },function(){
      draftNotice("下書き一覧を取得できません。未認証、または保存先に接続できません。");
    }).finally(function(){draftBusy=false;showDrafts.disabled=false;});
  }
  saveDraft.addEventListener("click",function(){
    const value=instruction.value.trim();
    if(!value){draftNotice("保存する文章を入力してください。");return;}
    if(draftBusy)return;
    saveDraft.disabled=true;
    draftRequest("/drafts","POST",{text:value}).then(function(){
      draftNotice("下書きを保存しました。TumblrやAIへは送信していません。");
    },function(){
      draftNotice("保存に失敗しました。入力欄の文章は保持されています。");
    }).finally(function(){saveDraft.disabled=false;});
  });
  showDrafts.addEventListener("click",refreshDrafts);
  instruction.addEventListener("keydown",function(event){
    if(event.key==="Enter"&&(event.ctrlKey||event.metaKey)){
      event.preventDefault();
      form.requestSubmit();
    }
  });
})();
