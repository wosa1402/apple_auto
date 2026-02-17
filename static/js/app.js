document.addEventListener('DOMContentLoaded', function() {

    // 密码显示/隐藏切换
    document.querySelectorAll('.btn-toggle-pw').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var row = this.closest('td');
            var mask = row.querySelector('.pw-mask');
            var text = row.querySelector('.pw-text');
            mask.classList.toggle('d-none');
            text.classList.toggle('d-none');
        });
    });

    // 删除账号
    document.querySelectorAll('.btn-delete').forEach(function(btn) {
        btn.addEventListener('click', function() {
            if (!confirm('确定要删除此账号吗？')) return;
            var id = this.dataset.id;
            fetch('/account/' + id + '/delete', {method: 'POST'})
                .then(function(r) { return r.json(); })
                .then(function() { location.reload(); })
                .catch(function() { alert('删除失败'); });
        });
    });

    // 启用/禁用切换
    document.querySelectorAll('.btn-toggle-enable').forEach(function(cb) {
        cb.addEventListener('change', function() {
            var id = this.dataset.id;
            fetch('/account/' + id + '/toggle', {method: 'POST'})
                .catch(function() { alert('切换失败'); });
        });
    });

    // 立即执行
    document.querySelectorAll('.btn-run-now').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var id = this.dataset.id;
            this.disabled = true;
            var self = this;
            fetch('/account/' + id + '/run', {method: 'POST'})
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.status) {
                        alert('任务已触发');
                    } else {
                        alert(data.message || '执行失败');
                    }
                    self.disabled = false;
                })
                .catch(function() {
                    alert('执行失败');
                    self.disabled = false;
                });
        });
    });

    // 删除代理
    document.querySelectorAll('.btn-delete-proxy').forEach(function(btn) {
        btn.addEventListener('click', function() {
            if (!confirm('确定要删除此代理吗？')) return;
            var id = this.dataset.id;
            fetch('/proxy/' + id + '/delete', {method: 'POST'})
                .then(function(r) { return r.json(); })
                .then(function() { location.reload(); })
                .catch(function() { alert('删除失败'); });
        });
    });

});
