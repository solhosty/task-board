import { QueryClient } from '@tanstack/react-query';
export const queryClient = new QueryClient({defaultOptions:{queries:{retry:1,refetchOnWindowFocus:true}}});
export async function api<T = any>(path:string, method='GET', body?:unknown):Promise<T> {
 const response=await fetch(path,{method,headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 const contentType=response.headers.get('content-type')||'';
 if(!contentType.includes('application/json')) throw new Error(`The server returned ${contentType||'an unexpected response'} for ${path}. Start the Python server and reload.`);
 const data=await response.json(); if(!response.ok) throw new Error(data.error||`Request failed (${response.status})`); return data;
}
export type Run={id:string;status:string;message:string;attempt_id?:number};
export type PR={id:number;url:string;number:number;state:string;review_state:string};
export type Task={id:number;project_id:number;text:string;status:string;workflow_stage?:string;board_position:number;run?:Run;session_count:number;harness_history:string[];active_harness?:string;integrity_status:string;pull_requests:PR[];execution_backend:string;execution_target_label:string;created_at:string;[key:string]:any};
export type ViewConfig={stages:string[];layout:string;density:string;fields:string[];sort:string};
export type SavedView={id:number;name:string;config:ViewConfig};
export type Project={id:number;name:string;repo_path:string;tasks:Task[];board_views:SavedView[];coder_profile:any;[key:string]:any};
export type Harness={key:string;label:string;enabled:number;installed:number;model:string;availability:{code:string;label:string;reason:string};[key:string]:any};
export type Bootstrap={api_version:number;projects:Project[];harnesses:Harness[];coder_servers:any[];adapters:Record<string,any>};
export const stages:Record<string,string>={planned:'Planned',running:'Running',review:'Needs review',done:'Done'};
export function stageOf(task:Task):string {if(task.workflow_stage)return task.workflow_stage; if(task.status==='completed'||task.run?.status==='complete')return 'done';if(['awaiting_review','awaiting_commit'].includes(task.run?.status||''))return 'review';if(['running','queued','verifying','rotating','committing','awaiting_dispatch','awaiting_capacity','awaiting_resume','paused_cooldown','awaiting_external_auth'].includes(task.run?.status||''))return 'running';return 'planned';}
export function attention(task:Task):string {if(task.integrity_status==='mismatch')return 'Workspace state needs review';return ({awaiting_review:'Review completed work',awaiting_commit:'Approve delivery',awaiting_dispatch:'Approve harness start',awaiting_resume:'Approve continuation',awaiting_external_auth:'Connect repository account',stopped:'Investigate stopped task',blocked:'Resolve execution blocker'} as Record<string,string>)[task.run?.status||'']||'';}
export function label(value?:string){return (value||'Ready').replaceAll('_',' ').replace(/^\w/,x=>x.toUpperCase());}
