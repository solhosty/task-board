import {useEffect,useRef,useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {Plus,FolderKanban,Activity,Brain,PlugZap,Settings,Menu,RefreshCw,CheckCircle2,AlertTriangle,Clock3,FolderPlus,GripVertical,Paperclip,X} from 'lucide-react';
import {DndContext,closestCenter,PointerSensor,KeyboardSensor,useSensor,useSensors,type DragEndEvent} from '@dnd-kit/core';
import {SortableContext,useSortable,sortableKeyboardCoordinates,verticalListSortingStrategy,arrayMove} from '@dnd-kit/sortable';
import {CSS} from '@dnd-kit/utilities';
import {api,queryClient,type HarnessOrderState,type Bootstrap,type Project,type Harness,type AdapterInfo,type ModelChoice,label,modelName,attention} from './lib/api';
import {attachmentName,readBase64,readableSize} from './lib/files';
import {Button} from './components/ui/button';
import {Modal,Field,Input,Textarea,Select,Checkbox,Empty} from './components/ui/controls';
import Board from './components/Board';
import {HarnessMark} from './components/BrandMark';
import TaskWorkspace from './components/TaskWorkspace';
import Memories from './components/Memories';
type Page='board'|'activity'|'memories'|'connections'|'settings';

export default function App(){
 const {data,error,isLoading,refetch}=useQuery({queryKey:['bootstrap'],queryFn:()=>api<Bootstrap>('/api/bootstrap')});
 const [page,setPage]=useState<Page>('board'),[projectId,setProjectId]=useState<number>(),[taskId,setTaskId]=useState<number>(),[newTask,setNewTask]=useState(false),[toast,setToast]=useState(''),[mobileOpen,setMobileOpen]=useState(false);
 const project=data?.projects.find(item=>item.id===(projectId??data?.projects[0]?.id));
 useEffect(()=>{if(!projectId&&data?.projects[0])setProjectId(data.projects[0].id)},[data,projectId]);
 const notify=(message:string)=>{setToast(message);window.setTimeout(()=>setToast(''),3000)};
 const refresh=()=>{queryClient.invalidateQueries({queryKey:['bootstrap']});refetch()};
 if(isLoading)return <div className="app-loading"><Brand markOnly/>Opening Aludra…</div>;
 if(error||!data)return <div className="app-loading"><AlertTriangle/><p>{(error as Error)?.message||'Unable to load Aludra.'}</p><Button onClick={()=>refetch()}>Try again</Button></div>;
 if(taskId&&project)return <TaskWorkspace project={project} taskId={taskId} onBack={()=>setTaskId(undefined)} onDeleted={()=>{setTaskId(undefined);refresh()}} refresh={refresh} notify={notify}/>;
 const subtitle:Record<Page,string>={board:'Task board',activity:'Activity',memories:'Memories',connections:'Connections',settings:'Project settings'};
 return <div className="app-shell">
  <aside className="rail">
   <Brand/>
   <nav><span className="rail-label">Workspace</span><Nav icon={<FolderKanban/>} label="Tasks" active={page==='board'} onClick={()=>setPage('board')}/><Nav icon={<Activity/>} label="Activity" active={page==='activity'} onClick={()=>setPage('activity')}/><Nav icon={<Brain/>} label="Memories" active={page==='memories'} onClick={()=>setPage('memories')}/></nav>
   <div className="rail-projects"><span className="rail-label">Projects</span>{data.projects.map(item=><button key={item.id} className={item.id===project?.id?'project-select active':'project-select'} onClick={()=>{setProjectId(item.id);setPage('board')}}><span>{item.name}</span><small>{item.tasks.length}</small></button>)}</div>
   <nav className="setup-nav"><span className="rail-label">Setup</span><Nav icon={<PlugZap/>} label="Connections" active={page==='connections'} onClick={()=>setPage('connections')}/><Nav icon={<Settings/>} label="Settings" active={page==='settings'} onClick={()=>setPage('settings')}/></nav>
   <div className="rail-footer"><b>Workspace ready</b><span>Coder connected · {data.harnesses.length} harnesses</span></div>
  </aside>
  <div className="mobile-top"><Brand/><Button variant="ghost" size="icon" aria-label="Menu"><Menu/></Button></div>
  <button className="mobile-menu-button" aria-label="Menu" onClick={()=>setMobileOpen(true)}><Menu size={22}/></button>
  {mobileOpen&&<div className="mobile-drawer"><button className="mobile-drawer-close" aria-label="Close menu" onClick={()=>setMobileOpen(false)}>×</button><Brand/><nav><span className="rail-label">Workspace</span><Nav icon={<FolderKanban/>} label="Tasks" active={page==='board'} onClick={()=>{setPage('board');setMobileOpen(false)}}/><Nav icon={<Activity/>} label="Activity" active={page==='activity'} onClick={()=>{setPage('activity');setMobileOpen(false)}}/><Nav icon={<Brain/>} label="Memories" active={page==='memories'} onClick={()=>{setPage('memories');setMobileOpen(false)}}/></nav><div className="rail-projects"><span className="rail-label">Projects</span>{data.projects.map(item=><button key={item.id} className={item.id===project?.id?'project-select active':'project-select'} onClick={()=>{setProjectId(item.id);setPage('board');setMobileOpen(false)}}><span>{item.name}</span><small>{item.tasks.length}</small></button>)}</div><nav><span className="rail-label">Setup</span><Nav icon={<PlugZap/>} label="Connections" active={page==='connections'} onClick={()=>{setPage('connections');setMobileOpen(false)}}/><Nav icon={<Settings/>} label="Settings" active={page==='settings'} onClick={()=>{setPage('settings');setMobileOpen(false)}}/></nav></div>}
  <main className="main">
   <header className="bar"><div className="crumb"><b>{project?.name||'Workspace'}</b><span>{subtitle[page]}</span></div><div className="actions"><Button variant="secondary" onClick={()=>setPage('settings')}>Project settings</Button><Button onClick={()=>setNewTask(true)}><Plus size={15}/>New task</Button></div></header>
   <section className="page">{page==='board'&&project&&<Board project={project} onOpen={setTaskId} onNew={()=>setNewTask(true)} refresh={refresh} notify={notify}/>} {page==='activity'&&<ActivityPage projects={data.projects} onOpen={setTaskId}/>} {page==='memories'&&<Memories notify={notify}/>} {page==='connections'&&<Connections data={data} refresh={refresh} notify={notify}/>} {page==='settings'&&<SettingsPage project={project} harnesses={data.harnesses} adapters={data.adapters} refresh={refresh} notify={notify}/>}</section>
  </main>
  <NewTask open={newTask} project={project} onClose={()=>setNewTask(false)} onCreated={id=>{setNewTask(false);refresh();setTaskId(id)}} notify={notify}/>
  {toast&&<div role="status" className="toast">{toast}</div>}
 </div>;
}
function Brand({markOnly=false}:{markOnly?:boolean}){return <div className={markOnly?'brand-mark':'brand'}><svg viewBox="0 0 190 125" aria-hidden="true"><path d="M18 106 L64 29 Q75 10 87 29 L136 106 L112 106 L76 49 L42 106 Z" fill="#5367FF"/><path d="M154 27 C154 38 160 44 171 44 C160 44 154 50 154 61 C154 50 148 44 137 44 C148 44 154 38 154 27 Z" fill="#F3C969"/></svg>{!markOnly&&<span>Aludra</span>}</div>}
function Nav({icon,label,active,onClick}:{icon:ReactNode;label:string;active:boolean;onClick:()=>void}){return <button className={'nav-item '+(active?'active':'')} onClick={onClick}>{icon}<span>{label}</span></button>}
function NewTask({open,project,onClose,onCreated,notify}:{open:boolean;project?:Project;onClose:()=>void;onCreated:(id:number)=>void;notify:(s:string)=>void}){
 const [text,setText]=useState(''),[permission,setPermission]=useState('inherit'),[memory,setMemory]=useState('inherit'),[executionTarget,setExecutionTarget]=useState('project'),[files,setFiles]=useState<File[]>([]);
 const fileInput=useRef<HTMLInputElement>(null);
 // The files travel with the create request rather than as follow-up uploads,
 // so a task that starts its harness immediately still begins with them.
 async function create(){if(!project)return;try{const attachments=await Promise.all(files.map(async file=>({filename:attachmentName(file),data:await readBase64(file)})));const result=await api<{id:number}>(`/api/projects/${project.id}/tasks`,'POST',{text,start:true,tool_permissions:permission,memory_mode:memory,execution_target:executionTarget,attachments});setFiles([]);setText('');setExecutionTarget('project');onCreated(result.id)}catch(error){notify((error as Error).message)}}
 const executionOptions:[string,string][]=[['project',`Use project default · ${project?.coder_profile?.default_target==='coder'?'Coder':'Local'} workspace`],['local','Local workspace'],...(project?.coder_profile?.enabled?[['coder','Coder workspace'] as [string,string]]:[])];
 return <Modal open={open} onClose={onClose} title="New task" description="Start with the outcome. Choose where this task runs when it needs an exception to the project default."><Field label="What should be done?"><Textarea autoFocus value={text} onChange={event=>setText(event.target.value)} placeholder="Describe the outcome and acceptance criteria…" onPaste={event=>{const pasted=Array.from(event.clipboardData.files);if(pasted.length){event.preventDefault();setFiles(current=>[...current,...pasted])}}}/></Field><Field label="Execution location" help="Local uses this machine. Coder runs in the project’s persistent remote workspace."><Select label="Execution location" value={executionTarget} onChange={setExecutionTarget} options={executionOptions}/></Field><Field label="Task permission mode"><Select label="Task permission mode" value={permission} onChange={setPermission} options={[[`inherit`,`Use project default`],[`ask`,`Ask before every tool`],[`standard`,`Standard harness permissions`],[`auto`,`Allow the harness to proceed`]]}/></Field><Field label="Memory" help="Codex only. Claude reads whatever is in its memory list either way."><Select label="Memory" value={memory} onChange={setMemory} options={[[`inherit`,`Use project default`],[`read_write`,`Read and add to memory`],[`read_only`,`Read memory, add nothing`],[`off`,`Ignore memory entirely`]]}/></Field><Field label="Files and images" help="Add them here or paste a screenshot into the box above. The harness receives them with the task."><input ref={fileInput} type="file" multiple hidden onChange={event=>{setFiles(current=>[...current,...Array.from(event.target.files||[])]);event.target.value=''}}/><Button variant="secondary" size="sm" onClick={()=>fileInput.current?.click()}><Paperclip size={15}/>Add files</Button>{files.length>0&&<div className="attachments">{files.map((file,index)=><figure className="attachment file" key={`${file.name}-${index}`}><span><Paperclip size={14}/><figcaption>{attachmentName(file)}<small>{readableSize(file.size)}</small></figcaption></span><button aria-label={`Remove ${attachmentName(file)}`} onClick={()=>setFiles(current=>current.filter((_,at)=>at!==index))}><X size={13}/></button></figure>)}</div>}</Field><p className="inherited"><b>Inherited setup</b><br/>Harness order, failover policy, environment, and verification come from <em>{project?.name||'this project'}</em>.</p><div className="actions"><Button variant="secondary" onClick={onClose}>Cancel</Button><Button disabled={!text.trim()||!project} onClick={create}>Create task</Button></div></Modal>;
}
function ActivityPage({projects,onOpen}:{projects:Project[];onOpen:(id:number)=>void}){const tasks=projects.flatMap(project=>project.tasks.map(task=>({...task,project:project.name}))).sort((a,b)=>b.id-a.id);return <><div className="pagehead"><div><p className="eyebrow">Workspace</p><h1>Activity</h1><p className="muted">Task events, ordered by when they happened.</p></div></div><div className="activity">{tasks.map(task=><button key={task.id} className="event" onClick={()=>onOpen(task.id)}><time className="time">Recently</time><span className={'eventmark '+(attention(task)?'attention':'')}>{attention(task)?<AlertTriangle/>:task.run?.status==='complete'?<CheckCircle2/>:<Clock3/>}</span><span><b>{task.text}</b><p>{task.run?.message||'Task created'} · {task.project}</p></span></button>)}{!tasks.length&&<Empty title="No activity yet"/>}</div></>}
function Connections({data,refresh,notify}:{data:Bootstrap;refresh:()=>void;notify:(s:string)=>void}){return <><div className="pagehead"><div><p className="eyebrow">Readiness</p><h1>Connections</h1><p className="muted">A connection is ready only when access, availability, and environment are all ready.</p></div></div><div className="grid2">{data.harnesses.map(harness=><article className="connection" key={harness.key}><div className="connectiontop"><HarnessMark name={harness.key} className="connectionlogo"/><b>{harness.label}</b><span className={'badge '+(harness.availability?.code==='ready'?'':'warn')}>{harness.availability?.label||'Unknown'}</span></div><p>{harness.availability?.reason||'Availability has not been checked.'}</p><Button variant="secondary" size="sm" onClick={async()=>{try{await api('/api/scan','POST',{});refresh();notify('Harness availability refreshed')}catch(error){notify((error as Error).message)}}}>Refresh status</Button></article>)}</div></>}
function SettingsPage({project,harnesses,adapters,refresh,notify}:{project?:Project;harnesses:Harness[];adapters:Record<string,AdapterInfo>;refresh:()=>void;notify:(s:string)=>void}){
 const [scope,setScope]=useState('global'),[category,setCategory]=useState('execution'),[name,setName]=useState(project?.name||''),[verify,setVerify]=useState(project?.verify_command||''),[mode,setMode]=useState(project?.default_mode||'supervised'),[failover,setFailover]=useState(Boolean(project?.auto_failover)),[defaultTarget,setDefaultTarget]=useState(project?.coder_profile?.default_target||'local'),[taskId,setTaskId]=useState<number>();
 useEffect(()=>{setName(project?.name||'');setVerify(project?.verify_command||'');setMode(project?.default_mode||'supervised');setFailover(Boolean(project?.auto_failover));setDefaultTarget(project?.coder_profile?.default_target||'local')},[project]);
 if(!project)return <Empty title="Choose a project first"/>;
 const currentProjectId=project.id;
 const task=project.tasks.find(item=>item.id===taskId)||project.tasks[0];
 async function save(){try{await api(`/api/projects/${currentProjectId}`,'POST',{name,verify_command:verify,default_mode:mode,auto_failover:failover?1:0});const profile=project?.coder_profile;if(profile?.enabled){await api(`/api/projects/${currentProjectId}/coder-profile`,'POST',{coder_server_id:profile.coder_server_id,setup_profile:profile.setup_profile,repo_url:profile.repo_url,base_ref:profile.base_ref,auth_provider_id:profile.auth_provider_id,template_name:profile.template_name,default_target:defaultTarget})}refresh();notify('Project settings saved')}catch(error){notify((error as Error).message)}}
 async function saveModel(path:string,harness:string,model:string|null){try{await api(path,'POST',{harness,model});refresh();notify(model?`Model saved for this ${scope==='project'?'project':'task'}`:'Model choice cleared; the wider scope decides again')}catch(error){notify((error as Error).message)}}
 async function saveTaskExecution(taskId:number,target:string){try{await api(`/api/tasks/${taskId}`,'POST',{execution_target:target});refresh();notify(target==='project'?'Task now uses the project execution default':`Task will run in the ${target==='coder'?'Coder':'local'} workspace`)}catch(error){notify((error as Error).message)}}
 const body=(()=>{
  if(scope==='global')return category==='sessions'?<SettingRow title="Harness order" source="Global default" description="Eligible harnesses are considered in this order. Drag to change which one is tried first, and choose the model each one runs."><HarnessOrder harnesses={harnesses} adapters={adapters} refresh={refresh} notify={notify}/></SettingRow>:<Empty title="Nothing global to set here"><p>{category==='execution'?'Run mode, failover, and verification are set per project or per task.':'Review and delivery settings are set per project or per task.'}</p></Empty>;
  if(scope==='project')return <>{category==='execution'?<><SettingRow title="Project name" source="Project value"><Input value={name} onChange={event=>setName(event.target.value)}/></SettingRow><SettingRow title="Default execution location" source="Project value" description={project.coder_profile?.enabled?'Every task that inherits the project default runs here. Individual tasks can choose a different location.':'Connect a Coder workspace to make remote execution available.'}><Select label="Default execution location" value={defaultTarget} onChange={setDefaultTarget} options={[[`local`,`Local workspace`],...(project.coder_profile?.enabled?[[`coder`,`Coder workspace`] as [string,string]]:[])]}/></SettingRow><SettingRow title="Run mode" source="Project value"><Select label="Run mode" value={mode} onChange={setMode} options={[[`supervised`,`Ask before starting and finishing`],[`unattended`,`Run and finish automatically`]]}/></SettingRow></>:category==='sessions'?<><SettingRow title="Harness order" source="Project value" description="Drag to override the global chain for this project. Failover walks this order too."><HarnessOrder harnesses={harnesses} adapters={adapters} refresh={refresh} notify={notify} scope={`project:${currentProjectId}`}/></SettingRow><SettingRow title="Harness models" source="Project value" description="Order is set above. Choose the model each harness runs for this project's work; inherited rows follow the global default."><HarnessModels harnesses={harnesses} adapters={adapters} chosen={key=>project.harness_models?.[key]?.source==='project'?project.harness_models[key].model:'inherit'} inherited={key=>({model:harnesses.find(item=>item.key===key)?.model||'default',source:'global'})} onSave={(key,model)=>saveModel(`/api/projects/${currentProjectId}/harness-models`,key,model)}/></SettingRow></>:<><SettingRow title="Verification command" source="Project value" description="Run after a harness reports it is done. A failure sends the task back for another pass."><Input value={verify} onChange={event=>setVerify(event.target.value)}/></SettingRow><SettingRow title="Failover policy" source="Project value"><Checkbox label="Continue automatically with the next eligible harness" checked={failover} onChange={setFailover}/></SettingRow></>}<div className="settings-save"><Button onClick={save}>Save project settings</Button></div></>;
  if(!task)return <Empty title="This project has no tasks yet"><p>Create a task to give it a model override of its own.</p></Empty>;
  const picker=<SettingRow title="Task" source="Task override" description="Overrides apply to this task alone and are saved as you choose them."><Select label="Task" value={String(task.id)} onChange={value=>setTaskId(Number(value))} options={project.tasks.map(item=>[String(item.id),item.text])}/></SettingRow>;
  if(category==='sessions')return <>{picker}<SettingRow title="Harness order" source="Task override" description="Drag to override this task's chain. It takes precedence over the project order."><HarnessOrder harnesses={harnesses} adapters={adapters} refresh={refresh} notify={notify} scope={`task:${task.id}`}/></SettingRow><SettingRow title="Harness models" source="Task override" description="Whichever harness this task ends up on, it runs the model chosen here."><HarnessModels harnesses={harnesses} adapters={adapters} chosen={key=>task.harness_models?.[key]||'inherit'} inherited={key=>project.harness_models?.[key]||{model:'default',source:'global'}} onSave={(key,model)=>saveModel(`/api/tasks/${task.id}/harness-models`,key,model)}/></SettingRow></>;
  if(category==='execution')return <>{picker}<SettingRow title="Execution location" source={task.execution_target==='project'||!task.execution_target?'Project default':'Task override'} description="Choose where this task runs. This setting does not move an already-running task."><Select label="Execution location" value={task.execution_target||'project'} onChange={target=>saveTaskExecution(task.id,target)} options={[[`project`,`Use project default · ${project.coder_profile?.default_target==='coder'?'Coder':'Local'} workspace`],[`local`,`Local workspace`],...(project.coder_profile?.enabled?[[`coder`,`Coder workspace`] as [string,string]]:[])]}/></SettingRow><SettingRow title="Run mode" source="Project value" description="Task-specific run mode is available in the task workspace."><p className="muted">{project.default_mode==='unattended'?'Runs automatically':'Asks before starting and finishing'}</p></SettingRow></>;
  return <>{picker}<Empty title="No task-level override here"><p>Verification and failover are inherited from the project; set them there.</p></Empty></>;
 })();
 return <><div className="pagehead"><div><p className="eyebrow">{project.name}</p><h1>Settings</h1><p className="muted">Choose a scope first. Every value shows where it comes from.</p></div></div><div className="settingslayout"><nav className="settingsnav"><button className={category==='execution'?'on':''} onClick={()=>setCategory('execution')}>Execution</button><button className={category==='sessions'?'on':''} onClick={()=>setCategory('sessions')}>Sessions</button><button className={category==='review'?'on':''} onClick={()=>setCategory('review')}>Review & delivery</button></nav><div className="settingbody"><div className="scope"><button className={scope==='global'?'on':''} onClick={()=>setScope('global')}>Global</button><button className={scope==='project'?'on':''} onClick={()=>setScope('project')}>Project</button><button className={scope==='task'?'on':''} onClick={()=>setScope('task')}>Task override</button></div>{body}</div></div></>;
}
function ModelSelect({harness,adapters,value,inherited,onChange}:{harness:Harness;adapters:Record<string,AdapterInfo>;value:string;inherited?:ModelChoice;onChange:(value:string)=>void}){
 const options:[string,string][]=[...(inherited?[[`inherit`,`Inherit ${inherited.source} · ${inherited.model}`] as [string,string]]:[]),...(adapters[harness.key]?.models||['default']).map(id=>[id,modelName(id)] as [string,string])];
 // A model saved before a scan, or typed by hand, still has to show its own value.
 if(!options.some(([id])=>id===value))options.push([value,value]);
 return <Select className="model-select" label={`Model for ${harness.label}`} value={value} onChange={onChange} options={options}/>;
}
function HarnessModels({harnesses,adapters,chosen,inherited,onSave}:{harnesses:Harness[];adapters:Record<string,AdapterInfo>;chosen:(key:string)=>string;inherited:(key:string)=>ModelChoice;onSave:(key:string,model:string|null)=>void}){
 // No position numbers here: the chain is ordered per scope in the row above,
 // and a second numbering that disagrees with it only misleads.
 return <ol className="harness-order harness-models">{harnesses.map(harness=><li key={harness.key}>
  <HarnessMark name={harness.key}/>
  <b>{harness.label}</b>
  <span className="harness-state">{harness.enabled&&harness.installed?'Eligible':'Unavailable'}</span>
  <div className="harness-model"><ModelSelect harness={harness} adapters={adapters} value={chosen(harness.key)} inherited={inherited(harness.key)} onChange={value=>onSave(harness.key,value==='inherit'?null:value)}/></div>
 </li>)}</ol>;
}
const orderSource:Record<string,string>={global:'Global default',project:'Project value',task:'Task override',inherited:'Inherited'};
/** One draggable chain for a scope: "global", "project:12" or "task:34".  A
 *  scope with nothing stored shows what it inherits and only begins overriding
 *  once it is dragged. */
function HarnessOrder({harnesses,adapters,refresh,notify,scope='global'}:{harnesses:Harness[];adapters:Record<string,AdapterInfo>;refresh:()=>void;notify:(s:string)=>void;scope?:string}){
 const queryKey=['harness-order',scope];
 const {data,error,isLoading}=useQuery({queryKey,queryFn:()=>api<HarnessOrderState>(`/api/harness-order?scope=${encodeURIComponent(scope)}`)});
 const [dragging,setDragging]=useState<string[]>();
 const sensors=useSensors(useSensor(PointerSensor,{activationConstraint:{distance:4}}),useSensor(KeyboardSensor,{coordinateGetter:sortableKeyboardCoordinates}));
 const byKey=Object.fromEntries(harnesses.map(item=>[item.key,item]));
 const order=dragging||data?.order||[];
 const reload=()=>{queryClient.invalidateQueries({queryKey});refresh()};
 async function drop(event:DragEndEvent){
  const {active,over}=event;
  if(!over||active.id===over.id)return;
  const next=arrayMove(order,order.indexOf(String(active.id)),order.indexOf(String(over.id)));
  setDragging(next);
  try{await api('/api/harness-order','POST',{scope,order:next});reload();notify(scope==='global'?'Harness order saved':'Harness order saved for this scope')}
  catch(problem){notify((problem as Error).message)}
  finally{setDragging(undefined)}
 }
 async function resetOrder(){
  try{await api(`/api/harness-order?scope=${encodeURIComponent(scope)}`,'DELETE');setDragging(undefined);reload();notify('Back to the inherited order')}
  catch(problem){notify((problem as Error).message)}
 }
 async function saveModel(key:string,model:string){
  try{await api(`/api/harnesses/${key}`,'POST',{model});refresh();notify('Global model saved')}
  catch(problem){notify((problem as Error).message)}
 }
 if(isLoading)return <p className="muted">Reading the harness chain…</p>;
 if(error)return <p className="muted">{(error as Error).message}</p>;
 return <><DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={drop}>
  <SortableContext items={order} strategy={verticalListSortingStrategy}>
   <ol className="harness-order">{order.map((key,index)=><HarnessRow key={key} id={key} position={index+1} harness={byKey[key]} adapters={adapters} onModel={scope==='global'?saveModel:undefined}/>)}</ol>
  </SortableContext>
 </DndContext>
 <div className="harness-order-foot"><span className="source">{orderSource[data?.source||'global']||'Inherited'}</span>{scope!=='global'&&data?.overridden&&<Button variant="ghost" size="sm" onClick={resetOrder}>Reset to inherited</Button>}</div></>;
}
function HarnessRow({id,harness,position,adapters,onModel}:{id:string;harness?:Harness;position:number;adapters:Record<string,AdapterInfo>;onModel?:(key:string,model:string)=>void}){
 const sortable=useSortable({id});
 if(!harness)return null;
 const ready=harness.enabled&&harness.installed;
 return <li ref={sortable.setNodeRef} style={{transform:CSS.Transform.toString(sortable.transform),transition:sortable.transition,opacity:sortable.isDragging?.5:1}} className={sortable.isDragging?'dragging':''}>
  <button className="harness-grip" aria-label={`Reorder ${harness.label}`} {...sortable.attributes} {...sortable.listeners}><GripVertical size={15}/></button>
  <span className="harness-position">{position}</span>
  <HarnessMark name={harness.key}/>
  <b>{harness.label}</b>
  <span className="harness-state">{ready?'Eligible':'Unavailable'}</span>
  {onModel&&<div className="harness-model"><ModelSelect harness={harness} adapters={adapters} value={harness.model||'default'} onChange={value=>onModel(harness.key,value)}/></div>}
 </li>;
}
function SettingRow({title,source,description,children}:{title:string;source:string;description?:string;children:ReactNode}){return <section className="settingrow"><div><b>{title}</b>{description&&<p>{description}</p>}<span className="source">{source}</span></div><div>{children}</div></section>}
