"use strict";
// Assistant-first screen. A module button is not required before sending.
// Same-origin /assistant only. Replies are text, never HTML.
(function init(){
  const messages=document.getElementById("messages");
  const form=document.getElementById("chat-form");
  const instruction=document.getElementById("instruction");
  const connection=document.getElementById("connection");
  const connect=document.getElementById("tumblr-connect");
  // Local, authenticated status only. It never probes or refreshes Tumblr.
  async function showOAuthStatus(){
    connection.classList.remove("visually-hidden");
    try{
      const response=await fetch("/oauth/status",{credentials:"same-origin"});
      if(!response.ok)throw new Error("oauth_status_unavailable");
      const state=await response.json();
      if(state.connected!==true){
        connection.textContent="Tumblr：未連携";
        return;
      }
      const expires=Number(state.expires_at);
      const expiry=Number.isFinite(expires)&&expires>0;
      if(expiry&&expires<=Date.now()/1000){
        connection.textContent="Tumblr：保存済み認可の期限経過（更新・再認可が必要な場合があります）";
      }else if(expiry&&expires<=Date.now()/1000+600){
        connection.textContent="Tumblr：認可の更新時期（実APIの権限は未検証）";
      }else{
        connection.textContent="Tumblr：連携情報保存済み（実APIの権限は未検証）";
      }
    }catch(error){
      connection.textContent="Tumblr：連携状態を確認できません";
    }
  }
  showOAuthStatus();
  connect.addEventListener("click",async function(){
    connect.disabled=true;
    try{
      const response=await fetch("/oauth/start",{method:"POST",credentials:"same-origin"});
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

  // Library/Viewer is local-only. It never invokes a Tumblr endpoint.
  const libraryButton=document.getElementById("show-library");
  let libraryFilter="all";
  let libraryOffset=0;
  const librarySelection=new Set();
  function libraryLink(url){
    try{
      const u=new URL(url);
      if(u.protocol!=="https:"||u.username||u.password)return null;
      const host=u.hostname.toLowerCase();
      if(host!=="tumblr.com"&&host!=="www.tumblr.com"&&!host.endsWith(".tumblr.com"))return null;
      return u.href;
    }catch(error){return null;}
  }
  function libraryMedia(url){
    try{
      const u=new URL(url);
      const host=u.hostname.toLowerCase();
      return u.protocol==="https:"&&!u.username&&!u.password&&
        (host==="media.tumblr.com"||host.endsWith(".media.tumblr.com"))?u.href:null;
    }catch(error){return null;}
  }
  async function libraryRequest(path,payload){
    const options={credentials:"same-origin"};
    if(payload){options.method="POST";options.headers={"Content-Type":"application/json"};options.body=JSON.stringify(payload);}
    const response=await fetch(path,options);
    if(!response.ok)throw new Error("library_http_"+response.status);
    return response.json();
  }
  async function libraryUpdate(ids,field,value){
    if(!ids.length)return;
    const result=await libraryRequest("/library/update",{candidate_ids:ids,field:field,value:value});
    if(result.verified!==true||result.updated!==ids.length)throw new Error("library_unverified");
    await showLibrary();
  }
  async function mediaHistory(){
    try{
      const data=await libraryRequest("/media/jobs");
      if(data.verified!==true||!Array.isArray(data.jobs))throw new Error("history_unverified");
      const section=document.createElement("section");section.className="library-panel result-view";
      const title=document.createElement("h3");title.textContent="MP4保存履歴（最新100件）";
      section.appendChild(title);
      for(const job of data.jobs){
        const line=document.createElement("div");line.className="library-controls";
        const text=document.createElement("span");
        text.textContent=job.candidate_id+" • "+job.state+
          (job.error_code?" • "+job.error_code:"")+
          (job.state==="completed"?" • "+job.output_bytes+"バイト":"");
        line.appendChild(text);
        if(job.state==="completed"&&/^[0-9a-f]{32}$/.test(job.id)){
          const link=document.createElement("a");
          link.href="/media/file/"+job.id;link.textContent="MP4を保存";
          line.appendChild(link);
        }
        section.appendChild(line);
      }
      if(!data.jobs.length){
        const note=document.createElement("p");note.textContent="保存履歴はまだありません。";
        section.appendChild(note);
      }
      messages.appendChild(section);messages.scrollTop=messages.scrollHeight;
    }catch(error){display("メディア保存履歴を取得できませんでした。","system");}
  }
  async function mediaSave(item,index,button){
    const selected=(item.media||[])[index];
    const direct=selected&&selected.type==="video"&&libraryMedia(selected.url);
    const postFallback=index===0&&item.type==="video"&&libraryLink(item.url);
    if(!direct&&!postFallback)return;
    if(!window.confirm("この動画の保存について、あなたが権利を持つか、権利者の許諾と利用規約上の保存許可を確認済みですか？ 未確認ならキャンセルしてください。Tumblr本番の投稿操作はしません。"))return;
    button.disabled=true;
    const old=button.textContent;
    button.textContent="確認・変換中";
    try{
      const data=await libraryRequest("/media/save",{
        candidate_id:item.candidate_id,media_index:index,rights_confirmed:true
      });
      if(data.verified!==true)throw new Error("save_unverified");
      display("MP4保存結果："+(data.state||data.job?.state||"completed")+
        "。メディア保存履歴から確認してください。","system");
    }catch(error){
      display("MP4保存できませんでした。権限・動画形式・空き容量・処理上限を確認してください。保存履歴に成否を記録しています。","system");
    }finally{button.disabled=false;button.textContent=old;}
  }
  async function libraryView(item,index){
    if(!item||!Array.isArray(item.media)||!item.media.length)return;
    const media=item.media.map(function(entry,originalIndex){return {entry,originalIndex};})
      .filter(function(m){return libraryMedia(m.entry.url);});
    if(!media.length)return;
    const section=document.createElement("section");section.className="library-viewer";
    const heading=document.createElement("h3");heading.textContent=item.title||"投稿のメディア";
    const body=document.createElement("div");body.className="library-viewer-media";
    const toolbar=document.createElement("div");toolbar.className="library-viewer-tools";
    let cursor=Math.max(0,media.findIndex(function(m){return m.originalIndex===(index||0);}));
    async function render(){
      body.replaceChildren();
      const entry=media[cursor].entry;
      const source=libraryMedia(entry.url);
      if(entry.type==="image"){
        const img=document.createElement("img");img.src=source;img.alt="投稿内の画像";img.loading="lazy";
        img.referrerPolicy="no-referrer";body.appendChild(img);
      }else{
        const video=document.createElement("video");
        video.controls=true;video.preload="none";video.src=source;
        video.setAttribute("playsinline","");video.referrerPolicy="no-referrer";
        video.style.maxWidth="100%";video.style.maxHeight="65vh";
        body.appendChild(video);
        const a=document.createElement("a");a.href=source;a.target="_blank";a.rel="noopener noreferrer";
        a.textContent="動画の参照元を開く";body.appendChild(a);
      }
      save.hidden=entry.type!=="video";
      position.textContent=(cursor+1)+" / "+media.length;
      try{
        const saved=await libraryRequest("/library/progress",{candidate_id:item.candidate_id,media_index:media[cursor].originalIndex});
        if(saved.verified!==true)throw new Error("not_saved");
      }catch(error){display("閲覧位置を保存できませんでした。","system");}
    }
    const prev=document.createElement("button");prev.type="button";prev.textContent="前へ";
    const next=document.createElement("button");next.type="button";next.textContent="次へ";
    const position=document.createElement("span");
    const close=document.createElement("button");close.type="button";close.textContent="閉じる";
    prev.addEventListener("click",function(){cursor=(cursor+media.length-1)%media.length;render();});
    next.addEventListener("click",function(){cursor=(cursor+1)%media.length;render();});
    close.addEventListener("click",function(){section.remove();});
    const save=document.createElement("button");save.type="button";save.textContent="MP4保存";
    save.addEventListener("click",function(){mediaSave(item,media[cursor].originalIndex,save);});
    toolbar.append(prev,position,next,save,close);section.append(heading,body,toolbar);
    messages.appendChild(section);messages.scrollTop=messages.scrollHeight;
    await render();
  }
  async function showLibrary(){
    let data;
    try{
      data=await libraryRequest("/library?filter="+encodeURIComponent(libraryFilter)+"&offset="+libraryOffset+"&limit=50");
      if(data.verified!==true||!Array.isArray(data.items))throw new Error("not_verified");
    }catch(error){display("ライブラリを読み込めませんでした。保存状態を変更していません。","system");return;}
    const section=document.createElement("section");section.className="library-panel result-view";
    const heading=document.createElement("h3");heading.textContent="ライブラリ（保存候補）";
    const controls=document.createElement("div");controls.className="library-controls";
    const filter=document.createElement("select");filter.setAttribute("aria-label","ライブラリ絞り込み");
    for(const [value,label] of [["all","すべて"],["unread","未読"],["seen","既読"],["favorite","お気に入り"],["watch_later","あとで見る"],["unavailable","取得不能"]]){
      const option=document.createElement("option");option.value=value;option.textContent=label;filter.appendChild(option);
    }
    filter.value=libraryFilter;
    filter.addEventListener("change",function(){libraryFilter=filter.value;libraryOffset=0;librarySelection.clear();showLibrary();});
    const total=document.createElement("span");total.textContent=String(data.total)+"件";
    const bulk=document.createElement("div");bulk.className="library-controls";
    const viewSelected=document.createElement("button");
    viewSelected.type="button";viewSelected.textContent="選択した画像・動画を閲覧";
    viewSelected.addEventListener("click",function(){
      const selected=data.items.filter(function(item){return librarySelection.has(item.candidate_id)&&item.state==="visible";});
      if(!selected.length){display("閲覧する候補を選択してください。","system");return;}
      const panel=document.createElement("section");panel.className="library-viewer";
      const title=document.createElement("h3");title.textContent="選択したメディア";
      panel.appendChild(title);
      const grid=document.createElement("div");grid.className="library-grid";
      let found=0;
      selected.forEach(function(item){
        (item.media||[]).forEach(function(media,index){
          const uri=libraryMedia(media.url);
          if(!uri||found>=200)return;
          found++;
          const card=document.createElement("div");card.className="library-card";
          const button=document.createElement("button");button.type="button";
          button.textContent=item.title+" ("+(index+1)+")";
          button.addEventListener("click",function(){libraryView(item,index);});
          card.appendChild(button);
          if(media.type==="image"){
            const image=document.createElement("img");image.src=uri;image.alt=item.title;
            image.className="library-thumb";image.loading="lazy";image.referrerPolicy="no-referrer";
            card.appendChild(image);
          }else{
            const note=document.createElement("span");note.textContent="動画参照（直接再生は未取得）";
            card.appendChild(note);
          }
          grid.appendChild(card);
        });
      });
      if(!found){display("選択された候補に閲覧可能なメディア参照がありません。","system");return;}
      const close=document.createElement("button");close.type="button";close.textContent="閉じる";
      close.addEventListener("click",function(){panel.remove();});
      panel.append(grid,close);messages.appendChild(panel);messages.scrollTop=messages.scrollHeight;
    });
    bulk.appendChild(viewSelected);
    const history=document.createElement("button");history.type="button";
    history.textContent="MP4保存履歴";history.addEventListener("click",mediaHistory);
    bulk.appendChild(history);
    for(const [label,field,value] of [["既読にする","seen",true],["未読にする","seen",false],["お気に入り","favorite",true],["お気に入り解除","favorite",false],["あとで見る","watch_later",true],["あとで見る解除","watch_later",false]]){
      const b=document.createElement("button");b.type="button";b.textContent=label;
      b.addEventListener("click",async function(){
        try{await libraryUpdate(Array.from(librarySelection),field,value);librarySelection.clear();}
        catch(error){display("選択項目を更新できませんでした。","system");}
      });bulk.appendChild(b);
    }
    controls.append(filter,total);
    const cards=document.createElement("div");cards.className="library-grid";
    for(const item of data.items){
      const card=document.createElement("article");card.className="library-card";
      const check=document.createElement("input");check.type="checkbox";check.checked=librarySelection.has(item.candidate_id);
      check.setAttribute("aria-label",(item.title||item.candidate_id)+"を選択");
      check.addEventListener("change",function(){if(check.checked)librarySelection.add(item.candidate_id);else librarySelection.delete(item.candidate_id);});
      const name=document.createElement("strong");name.textContent=item.title||"(タイトルなし)";
      const meta=document.createElement("small");meta.textContent=item.blog+" • "+item.date+" • "+(item.seen?"既読":"未読")+
        (item.favorite?" • ★お気に入り":"")+(item.watch_later?" • あとで見る":"");
      card.append(check,name,meta);
      const safePreview=libraryMedia(item.thumbnail_url)||(item.media||[]).map(function(m){return libraryMedia(m.url);}).find(Boolean);
      if(safePreview){
        const img=document.createElement("img");img.className="library-thumb";img.src=safePreview;img.alt="投稿のサムネイル";
        img.loading="lazy";img.referrerPolicy="no-referrer";card.appendChild(img);
      }
      const actions=document.createElement("div");actions.className="library-controls";
      const open=document.createElement("button");open.type="button";open.textContent="閲覧";
      open.disabled=!(item.media||[]).length||item.state!=="visible";
      open.addEventListener("click",function(){libraryView(item,item.last_media_index);});
      actions.appendChild(open);
      if(item.state==="visible"&&item.type==="video"&&!((item.media||[]).some(function(m){return m.type==="video"&&libraryMedia(m.url);}))){
        const fallback=document.createElement("button");
        fallback.type="button";fallback.textContent="MP4保存";
        fallback.title="直接動画URLがない投稿から許可済み取得経路を確認";
        fallback.addEventListener("click",function(){mediaSave(item,0,fallback);});
        actions.appendChild(fallback);
      }
      const source=libraryLink(item.url);
      if(source){const a=document.createElement("a");a.href=source;a.target="_blank";a.rel="noopener noreferrer";a.textContent="出典";actions.appendChild(a);}
      card.appendChild(actions);
      cards.appendChild(card);
    }
    if(!data.items.length){
      const empty=document.createElement("p");empty.textContent="該当する保存候補はありません。";cards.appendChild(empty);
    }
    const pager=document.createElement("div");pager.className="library-controls";
    const prev=document.createElement("button");prev.type="button";prev.textContent="前の50件";
    const next=document.createElement("button");next.type="button";next.textContent="次の50件";
    prev.disabled=libraryOffset===0;next.disabled=libraryOffset+data.items.length>=data.total;
    prev.addEventListener("click",function(){libraryOffset=Math.max(0,libraryOffset-50);librarySelection.clear();showLibrary();});
    next.addEventListener("click",function(){libraryOffset+=50;librarySelection.clear();showLibrary();});
    pager.append(prev,next);
    if(data.resume_item&&typeof data.resume_item.candidate_id==="string"){
      const resume=document.createElement("button");resume.type="button";resume.textContent="前回の続き";
      resume.addEventListener("click",function(){
        libraryView(data.resume_item,data.resume_item.last_media_index);
      });pager.appendChild(resume);
    }
    section.append(heading,controls,bulk,cards,pager);
    messages.appendChild(section);messages.scrollTop=messages.scrollHeight;
  }
  libraryButton.addEventListener("click",showLibrary);

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
              if(view.action==="tumblr.discover"&&typeof record.blog==="string"&&
                 /^[a-z0-9][a-z0-9-]{0,62}$/.test(record.blog)){
                const exclude=document.createElement("button");
                exclude.type="button";
                exclude.textContent="除外";
                exclude.className="discovery-exclude";
                exclude.setAttribute("aria-label",record.blog+"を候補から除外");
                exclude.addEventListener("click",async function(){
                  if(!window.confirm(record.blog+"をこのアプリの発見候補から除外しますか？ Tumblr側ではブロックしません。"))return;
                  exclude.disabled=true;
                  try{
                    const response=await fetch("/discovery/exclude",{
                      method:"POST",credentials:"same-origin",
                      headers:{"Content-Type":"application/json"},
                      body:JSON.stringify({blog:record.blog})
                    });
                    if(!response.ok)throw new Error("exclude_failed");
                    tr.remove();
                    display("候補を除外しました。Tumblr側のアカウント操作はしていません。","system");
                  }catch(error){
                    display("候補の除外に失敗しました。変更していません。","system");
                    exclude.disabled=false;
                  }
                });
                td.appendChild(exclude);
                if(record.type==="blog"){
                  const inspect=document.createElement("button");
                  inspect.type="button";
                  inspect.className="discovery-exclude";
                  inspect.textContent="投稿を見る";
                  inspect.setAttribute("aria-label",record.blog+"の投稿候補を取得");
                  inspect.addEventListener("click",function(){
                    if(!window.confirm(record.blog+"の公開投稿候補を公式APIから1ページ取得しますか？"))return;
                    inspect.disabled=true;
                    const text="検索して 対象: blog:"+record.blog;
                    lastText=text;display(text,"user");
                    post({message:text}).finally(function(){inspect.disabled=false;});
                  });
                  td.appendChild(inspect);
                }
                if(typeof record.candidate_id==="string"&&
                   /^[a-z0-9][a-z0-9-]{0,62}:[0-9]{1,25}$/.test(record.candidate_id)){
                  const check=document.createElement("button");
                  check.type="button";
                  check.className="discovery-exclude";
                  check.textContent="状態確認";
                  check.setAttribute("aria-label","この投稿が閲覧できるか1件だけ確認");
                  check.addEventListener("click",async function(){
                    if(!window.confirm("Tumblr公式APIへこの投稿1件の状態確認を行います。続行しますか？"))return;
                    check.disabled=true;
                    try{
                      const response=await fetch("/discovery/recheck",{
                        method:"POST",credentials:"same-origin",
                        headers:{"Content-Type":"application/json"},
                        body:JSON.stringify({candidate_id:record.candidate_id})
                      });
                      if(!response.ok)throw new Error("recheck_unavailable");
                      const result=await response.json();
                      if(result.verified!==true)throw new Error("unverified");
                      if(result.state==="unavailable"){
                        tr.remove();
                        display("この投稿は現在、公式APIから取得できません。保存状態を更新しました。","system");
                      }else if(result.state==="visible"){
                        display("投稿1件の閲覧可能状態を確認しました。","system");
                      }else throw new Error("unknown_state");
                    }catch(error){
                      display("投稿状態を確認できません。通信制限・認証・公開状態を確認してください。勝手に削除扱いにはしていません。","system");
                    }finally{check.disabled=false;}
                  });
                  td.appendChild(check);
                }
              }
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
    if(view.action==="tumblr.discover"&&typeof view.next_target==="string"){
      if(view.from_cache){
        const note=document.createElement("p");
        note.textContent="保存済み候補を表示しています。新しいAPI検索は実行していません。";
        section.appendChild(note);
      }
      if(view.complete){
        const note=document.createElement("p");
        note.textContent="今回の巡回範囲は確認済みです。再取得は明示操作のみです。";
        section.appendChild(note);
        const refresh=document.createElement("button");
        refresh.type="button";
        refresh.className="discovery-next";
        refresh.textContent="新しく確認";
        refresh.addEventListener("click",function(){
          if(!window.confirm("Tumblr公式APIへ1回だけ再取得を要求します。続行しますか？"))return;
          refresh.disabled=true;
          const text="検索して 対象: refresh:"+view.next_target;
          lastText=text;display(text,"user");
          post({message:text}).finally(function(){refresh.disabled=false;});
        });
        section.appendChild(refresh);
      }else{
        const next=document.createElement("button");
        next.type="button";
        next.className="discovery-next";
        next.textContent="続きを取得";
        next.setAttribute("aria-label","保存された巡回位置から次の候補を取得");
        next.addEventListener("click",function(){
          next.disabled=true;
          const text="検索して 対象: "+view.next_target;
          lastText=text;
          display(text,"user");
          post({message:text}).finally(function(){next.disabled=false;});
        });
        section.appendChild(next);
      }
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
