import {expect,test} from '@playwright/test';

const bootstrap={api_version:9,harnesses:[{key:'codex',label:'Codex',enabled:1,installed:1,model:'default',availability:{code:'ready',label:'Ready',reason:'Signed in'}},{key:'claude',label:'Claude Code',enabled:1,installed:1,model:'sonnet',availability:{code:'ready',label:'Ready',reason:'Signed in'}}],coder_servers:[],adapters:{codex:{models:['default','gpt-5-codex'],model_source:'Local Codex model cache',runnable:true},claude:{models:['default','opus','sonnet','haiku'],model_source:'Claude aliases',runnable:true}},projects:[{id:1,name:'Research harness',repo_path:'/tmp/repo',verify_command:'npm test',default_mode:'supervised',auto_failover:1,coder_profile:{},board_views:[],harness_models:{codex:{model:'default',source:'global'},claude:{model:'sonnet',source:'global'}},tasks:[{id:11,project_id:1,text:'Implement the mobile board',status:'pending',workflow_stage:'planned',board_position:0,session_count:1,harness_history:['codex'],integrity_status:'passed',pull_requests:[],harness_models:{},execution_target_label:'Local',execution_backend:'local',created_at:'2026-09-04',run:null},{id:12,project_id:1,text:'Review the delivery PR',status:'pending',workflow_stage:'review',board_position:1,session_count:2,harness_history:['codex','claude'],integrity_status:'passed',pull_requests:[{id:1,number:42,state:'open',review_state:'pending',url:'https://example.test/pr/42'}],harness_models:{claude:'opus'},execution_target_label:'Coder',execution_backend:'coder',created_at:'2026-09-04',run:{id:'run-12',status:'awaiting_review',message:'Verification passed'}}]}]};

const taskDetail={task:bootstrap.projects[0].tasks[1],run:bootstrap.projects[0].tasks[1].run,pull_requests:bootstrap.projects[0].tasks[1].pull_requests,blockers:[],lease:{backend:'coder',workspace_name:'research-runner'},messages:[{id:1,role:'user',content:'Review the delivery pull request and record the decision.',created_at:'2026-09-04T12:00:00Z'}],sessions:[{id:2,session_number:2,harness_key:'claude',model:'sonnet',status:'active',integrity_status:'passed',integrity_detail:'Git baseline matches the sealed handoff.'}],attempts:[]};
async function mockBootstrap(page:any){await page.route('**/api/bootstrap',route=>route.fulfill({contentType:'application/json',body:JSON.stringify(bootstrap)}));await page.route('**/api/tasks/12',route=>route.fulfill({contentType:'application/json',body:JSON.stringify(taskDetail)}));}

test('the real backend provides a JSON bootstrap response',async({request})=>{const response=await request.get('/api/bootstrap');expect(response.headers()['content-type']).toContain('application/json');expect((await response.json()).api_version).toBe(9);});
test('legacy ui paths resolve to the React product',async({page})=>{await mockBootstrap(page);await page.goto('/ui/index.html');await expect(page.getByRole('heading',{name:'Work, in context'})).toBeVisible();});
test('board filters, saved-view dialog, and task workspace render',async({page},testInfo)=>{await mockBootstrap(page);await page.goto('/');await expect(page.getByRole('heading',{name:'Work, in context'})).toBeVisible();if((page.viewportSize()?.width||0)<600){await expect(page.getByRole('heading',{name:'Implement the mobile board'})).toBeVisible();await page.screenshot({path:testInfo.outputPath('board.png'),fullPage:true});return}await expect(page.getByRole('heading',{name:'Implement the mobile board'})).toBeVisible();await page.screenshot({path:testInfo.outputPath('board.png'),fullPage:true});await page.getByRole('tab',{name:/Needs you/}).click();await expect(page.getByText('Review the delivery PR').first()).toBeVisible();await page.getByRole('button',{name:'Edit view'}).click();await expect(page.getByRole('dialog',{name:'Edit current view'})).toBeVisible();await page.getByLabel('View name').fill('Morning review');await page.getByRole('button',{name:'Save view'}).click();await page.getByRole('button',{name:'View task'}).first().click();await expect(page.getByRole('heading',{name:'Review the delivery PR'})).toBeVisible();await expect(page.getByRole('button',{name:'Open current session'})).toBeVisible();await page.screenshot({path:testInfo.outputPath('task-workspace.png'),fullPage:true});});
test('phone layout has no horizontal overflow',async({page},testInfo)=>{await mockBootstrap(page);await page.setViewportSize({width:390,height:844});await page.goto('/');await expect(page.getByRole('heading',{name:'Implement the mobile board'})).toBeVisible();expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();await page.screenshot({path:testInfo.outputPath('phone-layout.png'),fullPage:true});});
test('supporting pages preserve the approved layout rhythm',async({page},testInfo)=>{await mockBootstrap(page);await page.goto('/');for(const label of ['Activity','Connections','Settings']){await page.getByRole('button',{name:label,exact:true}).first().click();await expect(page.getByRole('heading',{name:label,exact:true})).toBeVisible();await page.screenshot({path:testInfo.outputPath(`${label.toLowerCase()}.png`),fullPage:true})}});
test('saved views and the responsive navigation follow the documented flows',async({page})=>{await mockBootstrap(page);await page.goto('/');if((page.viewportSize()?.width||0)<600){await page.getByRole('button',{name:'Menu'}).click();await expect(page.locator('.mobile-drawer').getByText('Workspace',{exact:true})).toBeVisible();await page.locator('.mobile-drawer').getByRole('button',{name:'Connections',exact:true}).click();await expect(page.getByRole('heading',{name:'Connections',exact:true})).toBeVisible();return}await page.getByRole('tab',{name:'+ View'}).click();await expect(page.getByRole('heading',{name:'Saved views',exact:true})).toBeVisible();await page.getByRole('button',{name:'Save current view'}).click();await expect(page.getByRole('dialog',{name:'Edit current view'})).toBeVisible();});
test('delivery tab uses the approved pull-request table',async({page})=>{await mockBootstrap(page);await page.goto('/');if((page.viewportSize()?.width||0)<600)return;await page.getByRole('tab',{name:'Pull requests'}).click();await expect(page.getByRole('heading',{name:'Pull requests',exact:true})).toBeVisible();await expect(page.getByText('#42 Review the delivery PR')).toBeVisible();await expect(page.getByRole('button',{name:'Open task'})).toBeVisible();});

test('every settings scope chooses the model each harness runs',async({page},testInfo)=>{
  await mockBootstrap(page);
  const saved:any[]=[];
  for(const route of ['**/api/harnesses/*','**/api/projects/1/harness-models','**/api/tasks/*/harness-models']){
    await page.route(route,async r=>{saved.push({url:r.request().url(),body:r.request().postDataJSON()});await r.fulfill({contentType:'application/json',body:'{}'})});
  }
  await page.goto('/');
  if((page.viewportSize()?.width||0)<600)return;
  await page.getByRole('button',{name:'Settings',exact:true}).first().click();
  await expect(page.getByText('Harness order',{exact:true})).toBeVisible();
  await page.getByLabel('Model for Claude Code').click();
  await page.getByRole('option',{name:'opus'}).click();
  expect(saved.at(-1)).toMatchObject({url:/\/api\/harnesses\/claude$/,body:{model:'opus'}});
  await page.screenshot({path:testInfo.outputPath('settings-global-models.png'),fullPage:true});

  await page.getByRole('button',{name:'Project',exact:true}).click();
  await expect(page.getByText('Harness models',{exact:true})).toBeVisible();
  await expect(page.getByLabel('Model for Claude Code')).toContainText('Inherit global · sonnet');
  await page.getByLabel('Model for Codex').click();
  await page.getByRole('option',{name:'gpt-5-codex'}).click();
  expect(saved.at(-1)).toMatchObject({url:/\/api\/projects\/1\/harness-models$/,body:{harness:'codex',model:'gpt-5-codex'}});

  await page.getByRole('button',{name:'Task override',exact:true}).click();
  await page.getByLabel('Task').click();
  await page.getByRole('option',{name:'Review the delivery PR'}).click();
  await expect(page.getByLabel('Model for Claude Code')).toContainText('opus');
  await page.getByLabel('Model for Claude Code').click();
  await page.getByRole('option',{name:/^Inherit global/}).click();
  expect(saved.at(-1)).toMatchObject({url:/\/api\/tasks\/12\/harness-models$/,body:{harness:'claude',model:null}});
  await page.screenshot({path:testInfo.outputPath('settings-task-models.png'),fullPage:true});
});
