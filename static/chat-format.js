// Small, deliberately HTML-free Markdown subset for harness replies.
function renderChatText(text) {
 const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const inline = value => value.split(/(`[^`\n]+`)/g).map(part => part.startsWith('`') && part.endsWith('`')
  ? '<code>'+escape(part.slice(1,-1))+'</code>'
  : escape(part).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')).join('');
 const lines=String(text).split('\n'),out=[];let code=null,paragraph=[],list=null;
 const flush=()=>{if(paragraph.length){out.push('<p>'+paragraph.map(inline).join('<br>')+'</p>');paragraph=[]}if(list){out.push('</'+list+'>');list=null}};
 for(const line of lines){
  if(/^\s*```/.test(line)){flush();if(code!==null){out.push('<pre><code>'+escape(code.join('\n'))+'</code></pre>');code=null}else code=[];continue}
  if(code!==null){code.push(line);continue}
  if(!line.trim()){flush();continue}
  const heading=line.match(/^#{1,6}\s+(.+)/),bullet=line.match(/^\s*(?:([-*])|\d+\.)\s+(.+)/);
  if(heading){flush();out.push('<h3>'+inline(heading[1])+'</h3>');continue}
  if(bullet){const type=bullet[1]?'ul':'ol';if(list!==type){flush();list=type;out.push('<'+type+'>')}out.push('<li>'+inline(bullet[2])+'</li>');continue}
  if(list)flush();paragraph.push(line);
 }
 flush();if(code!==null)out.push('<pre><code>'+escape(code.join('\n'))+'</code></pre>');return out.join('');
}
function renderHarnessEvents(events, replies=[], attemptId='turn') {
 const escape = value => String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 let html='',tools=[],batch=0,lastMessage=null;
 const flush=()=>{if(!tools.length)return;html+='<details class="tool-batch" data-key="batch-'+attemptId+'-'+batch+++'"><summary>'+tools.length+' tool '+(tools.length===1?'action':'actions')+'</summary>'+tools.join('')+'</details>';tools=[]};
 events.forEach((event,index)=>{
  if(event.kind==='permission'){
   flush();html+='<aside class="permission-request"><strong>'+escape(event.text)+'</strong><p>These tools were denied. The task needs permission before it can finish.</p><ul>'+(event.requests||[]).map(r=>'<li><span>'+escape(r.tool)+'</span><code>'+escape(r.command)+'</code></li>').join('')+'</ul></aside>';return;
  }
  if(event.kind==='message'){
   flush();const text=event.text.trim();
   if(text&&!replies.includes(text)&&text!==lastMessage)html+='<div class="event-message message-content">'+renderChatText(event.text)+'</div>';
   lastMessage=text;return;
  }
  if(event.kind==='reasoning'){
   flush();const text=event.text.trim();
   if(text&&text!==lastMessage)html+='<article class="reasoning-event"><div class="reasoning-label">Reasoning summary</div><div>'+renderChatText(text)+'</div></article>';
   lastMessage=text;return;
  }
  tools.push('<details class="tool-embed" data-key="'+attemptId+'-'+escape(event.event_id||index)+'"><summary><span class="tool-kind">'+escape(event.kind)+'</span><span>'+escape(event.text)+'</span><small>'+escape(event.status.replaceAll('_',' '))+'</small></summary><pre>'+escape(event.output||'No additional output recorded.')+'</pre></details>');
 });
 flush();return html;
}
if(typeof module!=='undefined')module.exports={renderChatText,renderHarnessEvents};
