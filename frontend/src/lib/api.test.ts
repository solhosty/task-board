import {afterEach,describe,expect,it,vi} from 'vitest';
import {api,taskRefreshInterval} from './api';

describe('api',()=>{
  afterEach(()=>vi.unstubAllGlobals());

  it('returns a JSON response',async()=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({api_version:9}),{headers:{'content-type':'application/json'}})));
    await expect(api('/api/bootstrap')).resolves.toEqual({api_version:9});
  });

  it('explains an HTML response instead of failing with a JSON parse error',async()=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response('<!doctype html>',{headers:{'content-type':'text/html'}})));
    await expect(api('/api/bootstrap')).rejects.toThrow('Start the Python server and reload');
  });

  it.each(['awaiting_dispatch','awaiting_capacity','awaiting_resume','paused_cooldown','awaiting_external_auth','blocked'])('keeps task details fresh while a run is %s',(status)=>{
    expect(taskRefreshInterval({run:{status}} as any)).toBe(2500);
  });
});
