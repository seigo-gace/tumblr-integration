"use strict";
// Assistant-first screen. A module button is not required before sending.
// Same-origin /assistant only. Replies are text, never HTML.
(function init(){
  const messages=document.getElementById("messages");
  const form=document.getElementById("chat-form");
  const instruction=document.getElementById("instruction");
  const connection=document.getElementById("connection");
  const connect=document.getElementById("tumblr-connect");
  connect.addEventListener("click",async function(){
    connect.disabled=true;
    try{
      let response=await fetch("/oauth/start",{method:"POST",credentials:"same-origin"});
      if(response.status===503){
        const blog=window.prompt("Tumblr運用ブログ名を入力してください（例: myblog）。個人用ブログは指定しないでください。");
        if(!blog){connect.disabled=false;return;}
        const setup=await fetch("/oauth/configure",{
          method:"POST",credentials:"same-origin",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({blog:blog.trim()})
        });
        if(!setup.ok)throw new Error("oauth_blog_setup_failed");
        response=await fetch("/oauth/start",{method:"POST",credentials:"same-origin"});
      }
      if(!response.ok)throw new Error("oauth_not_ready");
      const data=await response.json();
      const url=new URL(data.authorize_url);
      if(url.protocol!=="https:"||url.hostname!=="www.tumblr.com"||url.pathname!=="/oauth2/authorize")
        throw new Error("invalid_oauth_redirect");
      window.location.assign(url.href);
    }catch(error){
      display("Tumblr連携を開始できません。運用ブログと認証設定を確認してください。","system");
      connect.disabled=false;
    }
  });

  const assist=document.getElementById("assist");
  const confirm=document.getElementById("confirm");
  const attachmentPicker=document.getElementById("attachments");
  const attachmentList=document.getElementById("attachment-list");
  const sendButton=document.getElementById("send");
  const pendingFiles=[];
  let sending=false;
  function refreshAttachmentList(){
    attachmentList.replaceChildren();
    pendingFiles.forEach(function(file,index){
      const chip=document.createElement("div");
      chip.className="attachment-chip";
      if(file.type.startsWith("image/")){
        const image=document.createElement("img");
        const preview=URL.createObjectURL(file);
        image.src=preview;
        image.alt="";
        image.onload=function(){URL.revokeObjectURL(preview);};
        image.onerror=function(){URL.revokeObjectURL(preview);};
        chip.appendChild(image);
      }
      const title=document.createElement("span");
      title.textContent=file.name;
      const remove=document.createElement("button");
      remove.type="button";
      remove.textContent="×";
      remove.setAttribute("aria-label",file.name+"を添付から外す");
      remove.addEventListener("click",function(){
        pendingFiles.splice(index,1);
        refreshAttachmentList();
      });
      chip.append(title,remove);
      attachmentList.appendChild(chip);
    });
    instruction.required=selectedGoals.size===0 && pendingFiles.length===0;
  }
  attachmentPicker.addEventListener("change",function(){
    const chosen=Array.from(attachmentPicker.files||[]);
    for(const file of chosen){
      if(pendingFiles.length>=5){display("添付は一度に5件までです。","system");break;}
      if(file.size===0||file.size>8*1024*1024){
        display(file.name+"：添付できるサイズは1件8MBまでです。","system");
        continue;
      }
      if(file.name.length>120){
        display("ファイル名が長すぎるため添付できません。","system");
        continue;
      }
      pendingFiles.push(file);
    }
    attachmentPicker.value="";
    refreshAttachmentList();
  });
  function fileBase64(file){
    return new Promise(function(resolve,reject){
      const reader=new FileReader();
      reader.onload=function(){
        const data=String(reader.result||"");
        if(!data.includes(","))reject(new Error("invalid_file"));
        else resolve(data.slice(data.indexOf(",")+1));
      };
      reader.onerror=function(){reject(new Error("read_failed"));};
      reader.readAsDataURL(file);
    });
  }
  async function uploadAttachment(file){
    const encoded=await fileBase64(file);
    const response=await fetch("/attachments",{
      method:"POST",
      credentials:"same-origin",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:file.name,data:encoded})
    });
    if(!response.ok)throw new Error("upload_failed");
    return response.json();
  }
  function displayAttachments(records){
    if(!records.length)return;
    const bubble=document.createElement("div");
    bubble.className="message user attachment-message";
    for(const record of records){
      const row=document.createElement("div");
      row.className="sent-attachment";
      if(record.media_type.startsWith("image/")){
        const img=document.createElement("img");
        img.src="/attachments/"+encodeURIComponent(record.id);
        img.alt=record.name;
        row.appendChild(img);
      }
      const text=document.createElement("span");
      text.textContent=record.name;
      row.appendChild(text);
      bubble.appendChild(row);
    }
    messages.appendChild(bubble);
    messages.scrollTop=messages.scrollHeight;
  }
  const goalButtons=Array.from(document.querySelectorAll("#goal-grid .goal"));
  const goalSelection=document.getElementById("goal-selection");
  const clearGoals=document.getElementById("clear-goals");
  const selectedGoals=new Set();
  function renderGoals(){
    goalButtons.forEach(function(button){
      const selected=selectedGoals.has(button.dataset.goal);
      button.setAttribute("aria-pressed",selected?"true":"false");
      button.classList.toggle("active",selected);
    });
    goalSelection.textContent=selectedGoals.size
      ?"選択中："+Array.from(selectedGoals).join("・")
      :"";
    instruction.required=selectedGoals.size===0 && pendingFiles.length===0;
  }
  goalButtons.forEach(function(button){
    button.addEventListener("click",function(){
      const goal=button.dataset.goal;
      if(selectedGoals.has(goal))selectedGoals.delete(goal);
      else selectedGoals.add(goal);
      renderGoals();
    });
  });
  clearGoals.addEventListener("click",function(){selectedGoals.clear();renderGoals();});
  renderGoals();
  let lastText="";
  let pending=null;
  function display(text,role){
    const item=document.createElement("div");
    item.className="message "+role;
    item.textContent=text;
    messages.appendChild(item);
    messages.scrollTop=messages.scrollHeight;
  }
  // Render only the bounded, server-defined contract. No model HTML is interpreted.
  function renderView(view){
    if(!view||view.kind!=="records"||view.status!=="connected"||
       !Array.isArray(view.columns)||!Array.isArray(view.rows))return;
    const allowed=new Set(["title","type","date","url","label","value","period","name","status","thumbnail_url"]);
    const columns=view.columns.filter(function(key){return typeof key==="string"&&allowed.has(key);}).slice(0,6);
    if(!columns.length)return;
    const labels={title:"タイトル",type:"種類",date:"日付",url:"リンク",label:"項目",value:"値",period:"期間",name:"名前",status:"状態",thumbnail_url:"画像"};
    const section=document.createElement("section");
    section.className="result-view";
    const heading=document.createElement("h3");
    heading.textContent=String(view.title||"取得結果").slice(0,100);
    section.appendChild(heading);
    const rows=view.rows.slice(0,100).filter(function(item){return item&&typeof item==="object"&&!Array.isArray(item);});
    if(!rows.length){
      const empty=document.createElement("p");
      empty.textContent="取得できた表示項目はありません。";
      section.appendChild(empty);
    }else{
      const scroll=document.createElement("div");scroll.className="result-scroll";
      const table=document.createElement("table");table.className="result-table";
      const head=document.createElement("thead"),header=document.createElement("tr");
      columns.forEach(function(key){const th=document.createElement("th");th.textContent=labels[key];header.appendChild(th);});
      head.appendChild(header);table.appendChild(head);
      const body=document.createElement("tbody");
      rows.forEach(function(record){
        const tr=document.createElement("tr");
        columns.forEach(function(key){
          const td=document.createElement("td"),value=String(record[key]??"").slice(0,500);
          if(key==="thumbnail_url"&&value){
            try{
              const thumb=new URL(value);
              const host=thumb.hostname.toLowerCase();
              if(thumb.protocol!=="https:"||thumb.username||thumb.password||
                 !(host==="media.tumblr.com"||host.endsWith(".media.tumblr.com")))throw new Error("bad_thumbnail");
              const img=document.createElement("img");
              img.src=thumb.href;img.alt="投稿サムネイル";img.loading="lazy";img.referrerPolicy="no-referrer";
              img.className="result-thumbnail";
              td.appendChild(img);
            }catch(error){td.textContent="—";}
          }else if(key==="url"&&/^https:\/\/[^\s/@]+(?:\:[0-9]+)?(?:[/?#]|$)/i.test(value)){
            try{
              const url=new URL(value);
              if(url.protocol!=="https:"||url.username||url.password)throw new Error("bad_url");
              const link=document.createElement("a");link.href=url.href;link.target="_blank";link.rel="noopener noreferrer";link.textContent="開く";
              td.appendChild(link);
            }catch(error){td.textContent="リンク無効";}
          }else{td.textContent=value||"—";}
          tr.appendChild(td);
        });
        body.appendChild(tr);
      });
      table.appendChild(body);scroll.appendChild(table);section.appendChild(scroll);
    }
    // Data-derived chart, not AI-generated media. Only verified numeric analytics rows.
    if(view.action==="analytics.get_summary"&&rows.length){
      const numeric=rows.slice(0,20).map(function(record){
        const raw=String(record.value??"").replace(/,/g,"");
        return {label:String(record.label??"").slice(0,60),value:/^-?\d+(?:\.\d+)?$/.test(raw)?Number(raw):NaN};
      }).filter(function(item){return item.label&&Number.isFinite(item.value)&&item.value>=0;});
      const peak=Math.max(0,...numeric.map(function(item){return item.value;}));
      if(peak>0){
        const chart=document.createElement("div");chart.className="result-chart";
        chart.setAttribute("role","img");chart.setAttribute("aria-label","集計結果の棒グラフ");
        numeric.forEach(function(item){
          const line=document.createElement("div");line.className="result-bar-row";
          const label=document.createElement("span");label.textContent=item.label;
          const track=document.createElement("div");track.className="result-bar-track";
          const fill=document.createElement("div");fill.className="result-bar-fill";
          fill.style.width=(item.value/peak*100).toFixed(2)+"%";
          track.appendChild(fill);
          const number=document.createElement("span");number.textContent=String(item.value);
          line.append(label,track,number);chart.appendChild(line);
        });
        section.appendChild(chart);
      }
    }
    if(view.truncated){
      const note=document.createElement("p");note.textContent="先頭100件を表示しています。";section.appendChild(note);
    }
    messages.appendChild(section);
    messages.scrollTop=messages.scrollHeight;
  }
  function showAssist(turn){
    const open=Boolean(turn&&turn.recovery);
    assist.hidden=!open;
    confirm.hidden=!(turn&&turn.state==="confirmation_required"&&turn.request_id);
  }
  function apply(turn){
    display(turn.text||"応答を表示できません。実行していません。","system");
    renderView(turn.view);
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
  form.addEventListener("submit",async function(event){
    event.preventDefault();
    if(sending)return;
    const text=instruction.value.trim();
    const goals=Array.from(selectedGoals);
    const files=pendingFiles.slice();
    if(!text&&!goals.length&&!files.length)return;
    const message=goals.length
      ? "目的："+goals.join("・")+"\n"+(text||"この目的でできる操作と必要な確認を案内してください。")
      : text;
    sending=true;
    sendButton.disabled=true;
    try{
      const uploaded=[];
      for(const file of files)uploaded.push(await uploadAttachment(file));
      if(message){lastText=message;display(message,"user");}
      if(uploaded.length)displayAttachments(uploaded);
      instruction.value="";
      pendingFiles.splice(0,pendingFiles.length);
      refreshAttachmentList();
      await post({message:message,attachments:uploaded.map(function(item){return item.id;})});
    }catch(error){
      display("添付の送信に失敗しました。選択したファイルと入力文は残しています。","system");
    }finally{
      sending=false;
      sendButton.disabled=false;
    }
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
