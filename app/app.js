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
  instruction.addEventListener("keydown",function(event){
    if(event.key==="Enter"&&(event.ctrlKey||event.metaKey)){
      event.preventDefault();
      form.requestSubmit();
    }
  });
})();
