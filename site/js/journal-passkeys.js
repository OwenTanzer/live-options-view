// Encode WebAuthn fields explicitly for browsers predating credential.toJSON().
async function journalPasskey(invite) {
  const post = async body => {
    const r = await fetch('/api/trade-journal/passkey',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if (!r.ok) throw new Error((await r.json().catch(()=>({}))).error || 'Passkey request failed');
    return r.json();
  };
  const decode = s => Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));
  const encode = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
  const options = await post({action:'options',invite});
  options.challenge = decode(options.challenge);
  if (invite) options.user.id = decode(options.user.id);
  for (const c of options.allowCredentials || options.excludeCredentials || []) c.id = decode(c.id);
  const credential = await navigator.credentials[invite?'create':'get']({publicKey:options});
  const r = credential.response;
  const data = {clientDataJSON:encode(r.clientDataJSON)};
  if (invite) {
    data.attestationObject=encode(r.attestationObject);
    data.transports=r.getTransports?.() || [];
  } else {
    data.authenticatorData=encode(r.authenticatorData);
    data.signature=encode(r.signature);
    if(r.userHandle) data.userHandle=encode(r.userHandle);
  }
  await post({action:'verify',invite,response:{id:credential.id,rawId:encode(credential.rawId),type:credential.type,
    clientExtensionResults:credential.getClientExtensionResults(),response:data}});
}
