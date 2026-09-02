// ISKCON Shirpur — Motion & Interactive Client Script

document.addEventListener('DOMContentLoaded', () => {
    // 1. Auto dismiss alerts after 5s
    setTimeout(() => {
        const alerts = document.querySelectorAll('.alert');
        alerts.forEach(alert => {
            alert.style.opacity = '0';
            alert.style.transition = 'opacity 0.4s ease';
            setTimeout(() => alert.remove(), 400);
        });
    }, 5000);

    // 2. Scroll Reveal Animations with IntersectionObserver
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    if (!reducedMotion) {
        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('revealed');
                }
            });
        }, { threshold: 0.1 });

        document.querySelectorAll('.card, .table-wrapper, .reveal-on-scroll').forEach(el => {
            el.classList.add('reveal-on-scroll');
            observer.observe(el);
        });
    } else {
        document.querySelectorAll('.reveal-on-scroll, .card').forEach(el => {
            el.classList.add('revealed');
        });
    }

    // 3. Animated Stat Counters
    const counters = document.querySelectorAll('[data-counter]');
    counters.forEach(counter => {
        const target = parseFloat(counter.getAttribute('data-counter'));
        const prefix = counter.getAttribute('data-prefix') || '';
        const suffix = counter.getAttribute('data-suffix') || '';
        const duration = reducedMotion ? 0 : 1200;
        const startTime = performance.now();

        if (reducedMotion) {
            counter.innerText = `${prefix}${target.toLocaleString('en-IN', { maximumFractionDigits: 2 })}${suffix}`;
            return;
        }

        function updateCounter(currentTime) {
            const elapsed = currentTime - startTime;
            const progress = Math.min(elapsed / duration, 1);
            const easeOut = 1 - Math.pow(1 - progress, 3);
            const currentVal = target * easeOut;

            counter.innerText = `${prefix}${currentVal.toLocaleString('en-IN', { maximumFractionDigits: 2 })}${suffix}`;

            if (progress < 1) {
                requestAnimationFrame(updateCounter);
            }
        }
        requestAnimationFrame(updateCounter);
    });

    // 4. Initialize Analytics Charts if canvas present
    const trendCanvas = document.getElementById('predictiveTrendChart');
    if (trendCanvas) {
        fetch('/api/analytics-data')
            .then(res => res.json())
            .then(data => {
                const ctx = trendCanvas.getContext('2d');
                new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: data.labels,
                        datasets: [
                            {
                                label: 'Actual Daily Spend (₹)',
                                data: data.actual_data,
                                borderColor: '#e8952e',
                                backgroundColor: 'rgba(232, 149, 46, 0.1)',
                                fill: true,
                                tension: 0.35,
                                pointRadius: 4,
                                pointBackgroundColor: '#e8952e'
                            },
                            {
                                label: 'AI Predicted Forecast (₹)',
                                data: data.predicted_data,
                                borderColor: '#d4a843',
                                borderDash: [6, 6],
                                fill: false,
                                tension: 0.35,
                                pointRadius: 3,
                                pointBackgroundColor: '#d4a843'
                            }
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        animation: {
                            duration: reducedMotion ? 0 : 1500,
                            easing: 'easeOutQuart'
                        },
                        plugins: {
                            legend: { position: 'top' },
                            tooltip: { mode: 'index', intersect: false }
                        },
                        scales: {
                            y: {
                                beginAtZero: true,
                                grid: { color: '#f5efe6' }
                            },
                            x: {
                                grid: { display: false }
                            }
                        }
                    }
                });
            })
            .catch(err => console.error("Error loading analytics data:", err));
    }
});

function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.add('active');
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.remove('active');
}
