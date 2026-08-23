(()=>{
  const helpButton=document.getElementById('help-btn');
  const modalLayer=document.getElementById('modal-layer');
  const modalTitle=document.getElementById('modal-title');
  const modalBody=document.getElementById('modal-body');
  if(!helpButton||!modalLayer||!modalTitle||!modalBody) return;

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

  // app.js historically bound demo credentials into this button's help modal.
  // Loading this file last intentionally replaces that handler so the rendered
  // application shell never publishes packaged credentials to users.
  helpButton.onclick=openCredentialSafeHelp;
})();
