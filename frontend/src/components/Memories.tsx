import {useState} from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {useQuery} from '@tanstack/react-query';
import {Plus,Trash2,Pencil,ChevronRight,ArrowLeft,Globe,FolderKanban,StickyNote} from 'lucide-react';
import {api,queryClient,type MemoryOverview,type MemoryHarness,type MemoryScope,type MemoryEntry,type MemoryList} from '@/lib/api';
import {Button} from './ui/button';
import {Modal,Field,Input,Textarea,Select,Checkbox,Empty} from './ui/controls';
import {HarnessMark} from './BrandMark';

const typeLabels:Record<string,string>={user:'About you',feedback:'How to work',project:'This project',reference:'Reference'};
const blank={id:'',title:'',description:'',type:'project',body:''};

export default function Memories({notify}:{notify:(message:string)=>void}){
 const {data,error,isLoading,refetch}=useQuery({queryKey:['memories'],queryFn:()=>api<MemoryOverview>('/api/memories')});
 const [openKey,setOpenKey]=useState<string>();
 const reload=()=>{queryClient.invalidateQueries({queryKey:['memories']});queryClient.invalidateQueries({queryKey:['memory-entries']});refetch()};
 if(isLoading)return <div className="pagehead"><h1>Memories</h1><p className="muted">Reading the harness stores…</p></div>;
 if(error||!data)return <Empty title="Unable to read memory stores"><p>{(error as Error)?.message}</p></Empty>;
 const harness=data.harnesses.find(item=>item.key===openKey);
 if(harness)return <HarnessMemories harness={harness} types={data.types} onBack={()=>setOpenKey(undefined)} reload={reload} notify={notify}/>;
 return <>
  <div className="pagehead"><div><p className="eyebrow">Workspace</p><h1>Memories</h1><p className="muted">What each harness remembers between sessions. These are the files the harness reads when it starts, so edits apply to the next run.</p></div></div>
  <div className="memory-harnesses">{data.harnesses.map(item=>
   <button key={item.key} className="memory-harness" onClick={()=>setOpenKey(item.key)}>
    <HarnessMark name={item.key}/>
    <div><b>{item.label}</b><p>{item.summary}</p></div>
    <span className="memory-counts">{item.status==='disabled'?<span className="badge warn">Turned off</span>:<span className="meta">{item.scopes.reduce((total,scope)=>total+scope.count,0)} saved</span>}<ChevronRight size={16}/></span>
   </button>)}</div>
 </>;
}

function HarnessMemories({harness,types,onBack,reload,notify}:{harness:MemoryHarness;types:string[];onBack:()=>void;reload:()=>void;notify:(message:string)=>void}){
 const usable=harness.scopes.filter(scope=>scope.available);
 const [scopeKey,setScopeKey]=useState(usable[0]?.key||'global');
 const scope=harness.scopes.find(item=>item.key===scopeKey);
 const enabled=harness.status!=='disabled'&&Boolean(scope?.available);
 const {data,error}=useQuery({queryKey:['memory-entries',harness.key,scopeKey],enabled,
  queryFn:()=>api<MemoryList>(`/api/memories/${harness.key}?scope=${encodeURIComponent(scopeKey)}`)});
 const [editing,setEditing]=useState<typeof blank&{original_id?:string}>();
 const [noting,setNoting]=useState(false);

 async function remove(entry:MemoryEntry){
  if(!window.confirm(`Delete “${entry.title}”? The harness stops seeing it on its next run.`))return;
  try{await api(`/api/memories/${harness.key}/${entry.id}?scope=${encodeURIComponent(scopeKey)}`,'DELETE');reload();notify('Memory deleted')}
  catch(problem){notify((problem as Error).message)}
 }
 async function toggleGlobal(next:boolean){
  try{await api('/api/memories/claude/global','POST',{enabled:next});reload();setScopeKey(next?'global':(harness.scopes.find(item=>item.project_id)?.key||'global'));
   notify(next?'Claude now keeps one memory list for every project':'Claude is back to per-project memory')}
  catch(problem){notify((problem as Error).message)}
 }
 async function enableCodex(){
  try{await api('/api/memories/codex/enable','POST',{});reload();notify('Codex memories turned on')}
  catch(problem){notify((problem as Error).message)}
 }

 return <>
  <div className="pagehead memory-head">
   <div><Button variant="ghost" size="sm" onClick={onBack}><ArrowLeft size={14}/>All harnesses</Button>
    <h1><HarnessMark name={harness.key}/>{harness.label}</h1><p className="muted">{harness.summary}</p></div>
   {enabled&&<div className="actions">{harness.supports_notes&&<Button variant="secondary" onClick={()=>setNoting(true)}><StickyNote size={15}/>Add note</Button>}<Button onClick={()=>setEditing({...blank})}><Plus size={15}/>New memory</Button></div>}
  </div>

  {harness.status==='disabled'&&<div className="memory-callout"><div><b>Codex keeps memories turned off by default.</b><p>Turning them on sets <code>memories = true</code> in <code>~/.codex/config.toml</code>. Codex then builds its own store from your sessions, and anything you save here sits alongside it in its own folder.</p></div><Button onClick={enableCodex}>Turn on memories</Button></div>}

  {harness.key==='codex'&&harness.status!=='disabled'&&<div className="memory-callout subtle"><div><b>This is Codex's local memory only</b><p>If you are signed in with a ChatGPT account, Codex also draws on your ChatGPT account memory, which lives on OpenAI's servers and has no files on this machine. It cannot be read or edited from here — change it in ChatGPT under Settings → Personalization.</p></div></div>}

  {harness.key==='claude'&&<div className="memory-callout subtle"><div><b>Keep one list for every project</b><p>Claude stores memory per project directory unless a global directory is set. Turning this on points <code>autoMemoryDirectory</code> at a single folder, so everything you save here applies everywhere.</p></div><Checkbox label="Use one global memory directory" checked={harness.global_enabled} onChange={toggleGlobal}/></div>}

  {harness.status!=='disabled'&&<div className="settingslayout">
   <nav className="settingsnav memory-scopes">
    {harness.scopes.map(item=><button key={item.key} disabled={!item.available} className={item.key===scopeKey?'on':''} onClick={()=>setScopeKey(item.key)}>
     {item.project_id?<FolderKanban size={14}/>:<Globe size={14}/>}<span>{item.label}</span>{item.available&&<small>{item.count}</small>}</button>)}
   </nav>
   <div className="settingbody">
    {!scope?.available?<Empty title="Not available in this mode"><p>{harness.global_enabled?'Claude is using one global list, so per-project memories are turned off.':'Turn on the global directory to use this list.'}</p></Empty>
    :error?<Empty title="Unable to read this list"><p>{(error as Error).message}</p></Empty>
    :<>
     {data?.pending&&<p className="memory-note">Codex folds new entries into its memory when it next consolidates, so a change here may not show up in the very next run.</p>}
     <div className="memory-list">{(data?.entries||[]).map(entry=>
      <article key={entry.id} className="memory-entry">
       <div className="memory-entry-top"><span className={'memory-type '+entry.type}>{typeLabels[entry.type]||entry.type}</span>
        <div className="memory-entry-actions"><Button variant="ghost" size="icon" aria-label={`Edit ${entry.title}`} onClick={()=>setEditing({...entry,original_id:entry.id})}><Pencil size={14}/></Button>
         <Button variant="ghost" size="icon" aria-label={`Delete ${entry.title}`} onClick={()=>remove(entry)}><Trash2 size={14}/></Button></div></div>
       <b>{entry.title}</b>{entry.description&&<p className="memory-desc">{entry.description}</p>}
       <div className="memory-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.body}</ReactMarkdown></div>
      </article>)}
      {!data?.entries.length&&<Empty title="Nothing saved yet"><p>Add what this harness should carry between sessions — how you want it to work, or facts about the code it keeps rediscovering.</p></Empty>}
     </div>
     {data?.path&&<p className="memory-path">Stored in <code>{data.path}</code></p>}
    </>}
   </div>
  </div>}

  <EntryEditor value={editing} types={types} harness={harness.key} scope={scopeKey} onClose={()=>setEditing(undefined)} onSaved={()=>{setEditing(undefined);reload();notify('Memory saved')}} notify={notify}/>
  <NoteComposer open={noting} onClose={()=>setNoting(false)} onSaved={()=>{setNoting(false);notify('Note added for the next consolidation')}} notify={notify}/>
 </>;
}

function EntryEditor({value,types,harness,scope,onClose,onSaved,notify}:{value?:typeof blank&{original_id?:string};types:string[];harness:string;scope:string;onClose:()=>void;onSaved:()=>void;notify:(message:string)=>void}){
 const [draft,setDraft]=useState(value||blank);
 const key=value?(value.original_id||'new'):'closed';
 const [seen,setSeen]=useState(key);
 if(seen!==key){setSeen(key);setDraft(value||blank)}
 async function save(){
  try{await api(`/api/memories/${harness}`,'POST',{...draft,scope});onSaved()}
  catch(problem){notify((problem as Error).message)}
 }
 return <Modal open={Boolean(value)} onClose={onClose} title={value?.original_id?'Edit memory':'New memory'} description="Write it the way you would tell a new teammate. One fact per memory keeps it useful.">
  <Field label="Title"><Input autoFocus value={draft.title} onChange={event=>setDraft({...draft,title:event.target.value})} placeholder="Verification command"/></Field>
  <Field label="One-line summary" help="Shown in the index the harness reads first, so it decides what gets opened."><Input value={draft.description} onChange={event=>setDraft({...draft,description:event.target.value})} placeholder="How to run the test suite"/></Field>
  <Field label="Kind"><Select label="Kind" value={draft.type} onChange={type=>setDraft({...draft,type})} options={types.map(type=>[type,typeLabels[type]||type] as [string,string])}/></Field>
  <Field label="Memory"><Textarea value={draft.body} onChange={event=>setDraft({...draft,body:event.target.value})} placeholder="Run `python3 tests/smoke.py` before handing work back."/></Field>
  <div className="actions"><Button variant="secondary" onClick={onClose}>Cancel</Button><Button disabled={!draft.title.trim()||!draft.body.trim()} onClick={save}>Save memory</Button></div>
 </Modal>;
}

function NoteComposer({open,onClose,onSaved,notify}:{open:boolean;onClose:()=>void;onSaved:()=>void;notify:(message:string)=>void}){
 const [text,setText]=useState('');
 async function save(){
  try{await api('/api/memories/codex/notes','POST',{text});setText('');onSaved()}
  catch(problem){notify((problem as Error).message)}
 }
 return <Modal open={open} onClose={onClose} title="Add a note for Codex" description="Codex will not let anything edit its consolidated memory directly. A note is how you correct it — say what to remember, change, or forget, and it is treated as authoritative.">
  <Field label="Note"><Textarea autoFocus value={text} onChange={event=>setText(event.target.value)} placeholder="Forget the old deploy steps — we ship from the release workflow now."/></Field>
  <div className="actions"><Button variant="secondary" onClick={onClose}>Cancel</Button><Button disabled={!text.trim()} onClick={save}>Add note</Button></div>
 </Modal>;
}
