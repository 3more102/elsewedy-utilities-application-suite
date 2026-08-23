(()=>{
  const REPORT_DOCUMENT_CSP=[
    "default-src 'none'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "script-src 'none'",
    "script-src-attr 'none'",
    "connect-src 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'none'"
  ].join('; ');

  function hardenedReportDocument(source){
    const parser=new DOMParser();
    const documentNode=parser.parseFromString(String(source||''),'text/html');
    const head=documentNode.head;
    if(!head)throw new Error('Report HTML is missing a head element');

    head.querySelectorAll('base, meta[http-equiv]').forEach(element=>{
      if(element.tagName==='BASE'||String(element.getAttribute('http-equiv')||'').toLowerCase()==='content-security-policy'){
        element.remove();
      }
    });

    const policy=documentNode.createElement('meta');
    policy.httpEquiv='Content-Security-Policy';
    policy.content=REPORT_DOCUMENT_CSP;

    const referrer=documentNode.createElement('meta');
    referrer.name='referrer';
    referrer.content='no-referrer';

    const base=documentNode.createElement('base');
    base.href=location.origin+'/';

    // A meta-delivered CSP only governs content that follows it. Keep it first
    // so every report stylesheet/resource is covered inside the generated blob.
    head.prepend(base);
    head.prepend(referrer);
    head.prepend(policy);

    return '<!doctype html>'+documentNode.documentElement.outerHTML;
  }

  async function openProtectedReport(path){
    const response=await fetch(path,{headers:{Authorization:'Bearer '+S.token}});
    if(!response.ok)throw new Error('Unable to open report');

    const contentType=(response.headers.get('content-type')||'').split(';',1)[0].trim().toLowerCase();
    if(contentType!=='text/html')throw new Error('Protected report did not return HTML');

    const html=hardenedReportDocument(await response.text());
    const blob=new Blob([html],{type:'text/html'});
    const url=URL.createObjectURL(blob);
    try{
      window.open(url,'_blank','noopener,noreferrer');
    }finally{
      setTimeout(()=>URL.revokeObjectURL(url),60000);
    }
  }

  globalThis.openProtected=openProtectedReport;
  globalThis.EUASReportSecurity=Object.freeze({REPORT_DOCUMENT_CSP,hardenedReportDocument});
})();
