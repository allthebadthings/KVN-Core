const STORE='rook-ops-state-v1';
const statuses=[['todo','To do'],['in_progress','In progress'],['blocked','Blocked'],['approval','Needs approval'],['done','Done']];
const starter={tasks:[
{id:'ROOK-001',title:'Stand up shared operations state',owner:'Rook',priority:'high',status:'in_progress',attempts:1,cost:0,next_action:'Validate the board structure and assign owners to active KVN work.'},
{id:'ROOK-002',title:'Stop hidden retry loops',owner:'Rook',priority:'high',status:'todo',attempts:0,cost:0,next_action:'Enforce the circuit breaker after 2–3 meaningful failed attempts.'},
{id:'CODE-001',title:'Example blocked coding task',owner:'Coding',priority:'medium',status:'blocked',attempts:3,cost:4.25,next_action:'Rook must change strategy or reassign. Do not retry the same approach.'},
{id:'APP-001',title:'Example Kevin approval gate',owner:'Rook',priority:'high',status:'approval',attempts:1,cost:0.5,next_action:'Present one recommended decision and exactly what approval is required.'}
]};
let state=load();
const board=document.querySelector('#board'); const metrics=document.querySelector('#metrics');
const search=document.querySelector('#searchInput'); const filter=document.querySelector('#filterSelect'); const dialog=document.querySelector('#taskDialog');
function load(){try{return JSON.parse(localStorage.getItem(STORE))||structuredClone(starter)}catch{return structuredClone(starter)}}
function save(){localStorage.setItem(STORE,JSON.stringify(state));render()}
function esc(v=''){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function visible(t){const q=search.value.trim().toLowerCase();const f=filter.value;const blob=`${t.id} ${t.title} ${t.owner} ${t.next_action||''}`.toLowerCase();return(!q||blob.includes(q))&&(f==='all'||t.status===f)}
function render(){const tasks=state.tasks.filter(visible);const blocked=state.tasks.filter(t=>t.status==='blocked').length;const approval=state.tasks.filter(t=>t.status==='approval').length;const spend=state.tasks.reduce((a,t)=>a+(Number(t.cost)||0),0);metrics.innerHTML=`<div class="metric"><b>${state.tasks.length}</b><span>Total tasks</span></div><div class="metric"><b>${blocked}</b><span>Blocked</span></div><div class="metric"><b>${approval}</b><span>Need approval</span></div><div class="metric"><b>$${spend.toFixed(2)}</b><span>Tracked spend</span></div>`;board.innerHTML='';statuses.forEach(([key,label])=>{const col=document.createElement('section');col.className='column';const list=tasks.filter(t=>t.status===key);col.innerHTML=`<div class="column-head"><h2>${label}</h2><span class="count">${list.length}</span></div><div class="stack"></div>`;const stack=col.querySelector('.stack');list.forEach(t=>{const card=document.createElement('article');card.className=`task-card ${t.status}`;card.innerHTML=`<div class="task-meta"><span class="priority">${esc(t.priority)}</span><span class="owner">${esc(t.owner||'Unassigned')}</span></div><h3>${esc(t.title)}</h3><p class="next">${esc(t.next_action||'No next action recorded.')}</p><div class="task-footer"><span>${Number(t.attempts)||0} attempt${Number(t.attempts)===1?'':'s'}</span><span>$${(Number(t.cost)||0).toFixed(2)}</span></div>`;stack.appendChild(card)});board.appendChild(col)})}
search.addEventListener('input',render);filter.addEventListener('change',render);
document.querySelector('#newTaskBtn').onclick=()=>dialog.showModal();
document.querySelector('#resetBtn').onclick=()=>{state=structuredClone(starter);save()};
document.querySelector('#taskForm').addEventListener('submit',e=>{if(e.submitter?.value==='cancel')return;const title=document.querySelector('#taskTitle').value.trim();if(!title)return;e.preventDefault();state.tasks.unshift({id:`TASK-${Date.now().toString().slice(-6)}`,title,owner:document.querySelector('#taskOwner').value.trim()||'Unassigned',priority:document.querySelector('#taskPriority').value,status:document.querySelector('#taskStatus').value,attempts:0,cost:0,next_action:document.querySelector('#taskNext').value.trim()});save();e.target.reset();dialog.close()});
render();
