const confirmBox=document.getElementById('confirm');
const runButton=document.getElementById('run');
const copyButton=document.getElementById('copy');
const state=document.getElementById('state');
const result=document.getElementById('result');

confirmBox.onchange=()=>{runButton.disabled=!confirmBox.checked};

runButton.onclick=async()=>{
  runButton.disabled=true;
  confirmBox.disabled=true;
  copyButton.disabled=true;
  state.textContent='Running. The camera will make several small movements…';
  result.value='';
  try{
    const response=await fetch('/api/autotest/run',{method:'POST',cache:'no-store'});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||response.statusText);
    result.value=JSON.stringify(data,null,2);
    copyButton.disabled=false;
    state.textContent='Completed. Review the camera position, then copy the result.';
  }catch(error){
    state.textContent='Test failed: '+error;
    result.value=String(error);
  }finally{
    confirmBox.disabled=false;
    runButton.disabled=!confirmBox.checked;
  }
};

copyButton.onclick=async()=>{
  await navigator.clipboard.writeText(result.value);
  state.textContent='Complete result copied.';
};
