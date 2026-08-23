(()=>{
  const helpButton=document.getElementById('help-btn');
  const modalLayer=document.getElementById('modal-layer');
  const modalTitle=document.getElementById('modal-title');
  const modalBody=document.getElementById('modal-body');
  if(!helpButton||!modalLayer||!modalTitle||!modalBody) return;

  // The legacy monolithic UI still contains historical reference-account text
  // in more than one rendering path. Do not repeat packaged password values in
  // this guard; identify the historical username/password display shape and
  // replace it whenever legacy UI content is inserted into the DOM.
  const packagedCredentialPattern=/\b(?:omar|seif|planner|supervisor|tech1|tech2|store|proc|hse|exec)\s*\/\s*\S+@\d{4}\b/gi;

  function scrubCredentialText(root){
    if(!root) return;
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
    const nodes=[];
    while(walker.nextNode()) nodes.push(walker.currentNode);
    for(const node of nodes){
      const value=node.nodeValue||'';
      packagedCredentialPattern.lastIndex=0;
      if(packagedCredentialPattern.test(value)){
        packagedCredentialPattern.lastIndex=0;
        node.nodeValue=value.replace(packagedCredentialPattern,'your assigned EUAS account');
      }
    }
  }

  function openCredentialSafeHelp(){
    modalTitle.textContent='EUAS Help';
    modalBody.innerHTML=`
      <p class="section-sub">Use the left navigation or Application Launchpad to open a module. Global Search finds connected assets, work orders, documents and inspections. Select a site to focus portfolio views.</p>
      <div class="detail-grid">
        <div class="detail-box"><span>Authentication</span><strong>Use your assigned EUAS account</strong></div>
        <div class="detail-box"><span>Production access</span><strong>Credentials are managed by your administrator</strong></div>
      </div>`;
    modalLayer.classList.remove('hidden');
  }

  // app.js historically bound demo credentials into the Help handler and can
  // also render reference-account text in role-specific empty states. Loading
  // this file last replaces Help and observes later DOM writes so neither path
  // renders packaged credentials to the user.
  helpButton.onclick=openCredentialSafeHelp;
  scrubCredentialText(document.body);
  new MutationObserver(records=>{
    for(const record of records){
      if(record.type==='characterData') scrubCredentialText(record.target.parentNode);
      for(const node of record.addedNodes){
        if(node.nodeType===Node.TEXT_NODE){
          const value=node.nodeValue||'';
          packagedCredentialPattern.lastIndex=0;
          if(packagedCredentialPattern.test(value)){
            packagedCredentialPattern.lastIndex=0;
            node.nodeValue=value.replace(packagedCredentialPattern,'your assigned EUAS account');
          }
        }else if(node.nodeType===Node.ELEMENT_NODE){
          scrubCredentialText(node);
        }
      }
    }
  }).observe(document.body,{subtree:true,childList:true,characterData:true});
})();
