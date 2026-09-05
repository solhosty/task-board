// Attachments travel as base64 in the JSON API: one shared reader keeps the
// composer and the new-task dialog reading files the same way.
export type Attachment={id:number;message_id:number|null;filename:string;media_type:string;kind:'image'|'file';byte_size:number};
export function readableSize(size:number){return size>=1048576?`${(size/1048576).toFixed(1)} MB`:`${Math.max(Math.round(size/1024),1)} KB`}
export function readBase64(file:File){return new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error(`${file.name} could not be read`));reader.onload=()=>resolve(String(reader.result));reader.readAsDataURL(file)})}
// A pasted screenshot often arrives as a nameless blob, or as "image.png"
// from one browser and nothing from another. The server decides what it will
// accept by extension, so give every pasted file a name it can classify.
const PASTED_EXTENSIONS:Record<string,string>={'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp','application/pdf':'pdf','text/plain':'txt','text/markdown':'md','text/csv':'csv','application/json':'json'};
export function attachmentName(file:File){if(/\.[A-Za-z0-9]{1,8}$/.test(file.name||''))return file.name;const extension=PASTED_EXTENSIONS[file.type];return extension?`pasted-${new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')}.${extension}`:(file.name||'attachment');}
