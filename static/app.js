document.querySelector('.mobile-menu')?.addEventListener('click',()=>document.querySelector('.sidebar')?.classList.toggle('open'));
document.querySelectorAll('form[data-confirm]').forEach(form=>form.addEventListener('submit',event=>{if(!confirm(form.dataset.confirm))event.preventDefault()}));
document.querySelectorAll('form.submit-lock').forEach(form=>form.addEventListener('submit',event=>{const password=form.querySelector('[name=password]'),confirmField=form.querySelector('[name=password_confirm]');if(password&&confirmField&&password.value!==confirmField.value){event.preventDefault();alert('비밀번호 확인이 일치하지 않습니다.');return}const button=form.querySelector('button[type=submit],button:not([type])');if(button){button.disabled=true;button.dataset.original=button.textContent;button.textContent='처리 중…'}}));
setTimeout(()=>document.querySelectorAll('.toast').forEach(el=>el.classList.add('fade')),6000);

