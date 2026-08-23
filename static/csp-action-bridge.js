(()=>{
  const ACTION_SIGNATURES=Object.freeze({
    assetDetail:'n',
    editAsset:'n',
    workDetail:'n',
    openReportSnapshot:'n',
    toggleTask:'nn',
    issueReservation:'nn',
    releaseReservation:'nn',
    editTechnicianProfile:'n',
    assignTechnicianShift:'n',
    inventoryTx:'n',
    inventoryHistory:'n',
    submitPR:'n',
    approvePR:'n',
    addQuote:'n',
    makePO:'n',
    receivePO:'n',
    decideApproval:'ns',
    approvalHistory:'n',
    deactivateDelegation:'n',
    closeOutage:'n',
    go:'s',
    ackAlarm:'n',
    alarmToWork:'n',
    closeAlarm:'n',
    dispatchWork:'n',
    dispatchTransition:'ns',
    fieldAction:'ns',
    fieldPart:'n',
    fieldRead:'n',
    fieldNote:'n',
    inspectionDetail:'n',
    manageHSE:'n',
    editProjectTask:'nn',
    addProjectTask:'n',
    downloadDoc:'ns',
    editSlaPolicy:'n',
    retryOutbox:'n',
    editRetention:'n',
    toggleUser:'nn'
  });

  const CALL=/^\s*([A-Za-z_$][\w$]*)\((.*)\)\s*$/s;
  const NUMBER=/^-?\d+(?:\.\d+)?$/;

  function parseNumber(value){
    const text=String(value).trim();
    if(!NUMBER.test(text))return null;
    const number=Number(text);
    return Number.isFinite(number)?number:null;
  }

  function parseQuoted(value){
    const text=String(value).trim();
    if(text.length<2)return null;
    const quote=text[0];
    if((quote!=="'"&&quote!=='"')||text[text.length-1]!==quote)return null;
    return text.slice(1,-1);
  }

  function splitNumberAndString(value){
    const match=String(value).match(/^\s*(-?\d+(?:\.\d+)?)\s*,\s*([\s\S]+)\s*$/);
    if(!match)return null;
    const number=parseNumber(match[1]);
    const text=parseQuoted(match[2]);
    return number===null||text===null?null:[number,text];
  }

  function splitTwoNumbers(value){
    const match=String(value).match(/^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/);
    if(!match)return null;
    const first=parseNumber(match[1]);
    const second=parseNumber(match[2]);
    return first===null||second===null?null:[first,second];
  }

  function parseLegacyAction(source){
    const match=String(source||'').match(CALL);
    if(!match)return null;
    const name=match[1];
    const signature=ACTION_SIGNATURES[name];
    if(!signature)return null;
    const raw=match[2];
    let args=null;
    if(signature==='n'){
      const value=parseNumber(raw);
      args=value===null?null:[value];
    }else if(signature==='nn'){
      args=splitTwoNumbers(raw);
    }else if(signature==='s'){
      const value=parseQuoted(raw);
      args=value===null?null:[value];
    }else if(signature==='ns'){
      args=splitNumberAndString(raw);
    }
    return args?{name,args}:null;
  }

  function migrateElement(element){
    if(!(element instanceof Element)||!element.hasAttribute('onclick'))return;
    const source=element.getAttribute('onclick');
    const parsed=parseLegacyAction(source);
    element.removeAttribute('onclick');
    if(!parsed){
      element.dataset.euasActionRejected='true';
      console.error('EUAS blocked unsupported legacy inline action',source);
      return;
    }
    element.dataset.euasAction=parsed.name;
    element.dataset.euasArgs=encodeURIComponent(JSON.stringify(parsed.args));
  }

  function migrateTree(root){
    if(!(root instanceof Element))return;
    migrateElement(root);
    root.querySelectorAll('[onclick]').forEach(migrateElement);
  }

  document.querySelectorAll('[onclick]').forEach(migrateElement);
  new MutationObserver(records=>{
    for(const record of records){
      if(record.type==='attributes'){
        migrateElement(record.target);
        continue;
      }
      record.addedNodes.forEach(node=>migrateTree(node));
    }
  }).observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['onclick']});

  document.addEventListener('click',event=>{
    const trigger=event.target.closest?.('[data-euas-action]');
    if(!trigger)return;
    const name=trigger.dataset.euasAction;
    if(!ACTION_SIGNATURES[name])return;
    let args;
    try{
      args=JSON.parse(decodeURIComponent(trigger.dataset.euasArgs||'%5B%5D'));
    }catch{
      return;
    }
    if(!Array.isArray(args))return;
    const action=globalThis[name];
    if(typeof action!=='function'){
      console.error(`EUAS delegated action is unavailable: ${name}`);
      return;
    }
    event.preventDefault();
    Promise.resolve(action(...args)).catch(error=>{
      if(typeof globalThis.toast==='function')globalThis.toast(error?.message||String(error));
      else console.error(error);
    });
  });
})();
