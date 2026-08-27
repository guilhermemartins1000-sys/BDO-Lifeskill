
(function(){
  const KEY='bdo_harmonia_client_id';
  let clientId='';
  try {
    clientId=sessionStorage.getItem(KEY);
    if(!clientId){
      clientId=(crypto&&crypto.randomUUID)?crypto.randomUUID():('bdo-'+Date.now()+'-'+Math.random().toString(36).slice(2));
      sessionStorage.setItem(KEY,clientId);
    }
  } catch(e) { clientId='bdo-'+Date.now()+'-'+Math.random().toString(36).slice(2); }
  const ping=()=>{
    try{
      fetch('/api/heartbeat?client='+encodeURIComponent(clientId),{cache:'no-store',keepalive:true}).catch(()=>{});
    }catch(e){}
  };
  ping();
  const timer=setInterval(ping,5000);
  window.addEventListener('pagehide',()=>{
    clearInterval(timer);
    // Último ping é uma tentativa de manter o estado atualizado durante
    // navegações; o watchdog é quem confirma que a aba realmente sumiu.
    try{fetch('/api/heartbeat?client='+encodeURIComponent(clientId),{cache:'no-store',keepalive:true}).catch(()=>{});}catch(e){}
  },{once:true});
})();

// V33 — recolher/mostrar cabeçalho sem esconder a navegação
(function(){
  const h=document.getElementById("siteHeader"), b=document.getElementById("toggleSiteHeader");
  if(!h||!b)return;
  let collapsed=false; try{collapsed=localStorage.getItem("bdo_header_collapsed")==="1"}catch(e){}
  function apply(){h.classList.toggle("header-collapsed",collapsed);b.setAttribute("aria-expanded",String(!collapsed));b.title=collapsed?"Mostrar cabeçalho":"Esconder cabeçalho";}
  b.addEventListener("click",function(){collapsed=!collapsed;try{localStorage.setItem("bdo_header_collapsed",collapsed?"1":"0")}catch(e){}apply();});
  apply();
})();
