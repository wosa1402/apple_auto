document.addEventListener('DOMContentLoaded', function() {

    // Password show/hide toggle
    document.querySelectorAll('.btn-toggle-pw').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var row = this.closest('td');
            var mask = row.querySelector('.pw-mask');
            var text = row.querySelector('.pw-text');
            mask.classList.toggle('d-none');
            text.classList.toggle('d-none');
        });
    });

    // Delete account
    document.querySelectorAll('.btn-delete').forEach(function(btn) {
        btn.addEventListener('click', function() {
            if (!confirm('Delete this account?')) return;
            var id = this.dataset.id;
            fetch('/account/' + id + '/delete', {method: 'POST'})
                .then(function(r) { return r.json(); })
                .then(function() { location.reload(); })
                .catch(function() { alert('Delete failed'); });
        });
    });

    // Toggle enable/disable
    document.querySelectorAll('.btn-toggle-enable').forEach(function(cb) {
        cb.addEventListener('change', function() {
            var id = this.dataset.id;
            fetch('/account/' + id + '/toggle', {method: 'POST'})
                .catch(function() { alert('Toggle failed'); });
        });
    });

    // Run now
    document.querySelectorAll('.btn-run-now').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var id = this.dataset.id;
            this.disabled = true;
            var self = this;
            fetch('/account/' + id + '/run', {method: 'POST'})
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.status) {
                        alert('Task triggered');
                    } else {
                        alert(data.message || 'Failed');
                    }
                    self.disabled = false;
                })
                .catch(function() {
                    alert('Failed');
                    self.disabled = false;
                });
        });
    });

    // Delete proxy
    document.querySelectorAll('.btn-delete-proxy').forEach(function(btn) {
        btn.addEventListener('click', function() {
            if (!confirm('Delete this proxy?')) return;
            var id = this.dataset.id;
            fetch('/proxy/' + id + '/delete', {method: 'POST'})
                .then(function(r) { return r.json(); })
                .then(function() { location.reload(); })
                .catch(function() { alert('Delete failed'); });
        });
    });

});
