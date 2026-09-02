/**
 * ISKCON Shirpur — Spiritual Frontend Components
 * SpiritualSplashScreen, FlowerPetalAnimation, dashboard micro-interactions
 */

(function () {
    'use strict';

    const SPLASH_KEY = 'iskcon_shirpur_splash_seen';
    const SPLASH_DURATION_MS = 4500;
    const MAX_PETALS = 22;

    const prefersReducedMotion = () =>
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    /* ------------------------------------------------------------------ */
    /* FlowerPetalAnimation — lightweight pooled petal system             */
    /* ------------------------------------------------------------------ */
    class FlowerPetalAnimation {
        constructor(container, options = {}) {
            this.container = container;
            this.maxPetals = options.maxPetals || MAX_PETALS;
            this.intensity = options.intensity || 1;
            this.petals = [];
            this.running = false;
            this.spawnTimer = null;
            this.types = ['marigold', 'rose', 'lotus', 'white'];
        }

        start() {
            if (this.running || !this.container) return;
            this.running = true;
            this._spawnLoop();
        }

        stop() {
            this.running = false;
            if (this.spawnTimer) {
                clearTimeout(this.spawnTimer);
                this.spawnTimer = null;
            }
            this.petals.forEach(p => p.el.remove());
            this.petals = [];
        }

        _spawnLoop() {
            if (!this.running) return;
            if (this.petals.length < this.maxPetals) {
                this._createPetal();
            }
            const delay = prefersReducedMotion()
                ? 800
                : (400 + Math.random() * 600) / this.intensity;
            this.spawnTimer = setTimeout(() => this._spawnLoop(), delay);
        }

        _createPetal() {
            const type = this.types[Math.floor(Math.random() * this.types.length)];
            const el = document.createElement('div');
            el.className = `flower-petal flower-petal--${type}`;
            el.setAttribute('aria-hidden', 'true');

            const size = 0.7 + Math.random() * 0.8;
            const startX = Math.random() * 100;
            const duration = prefersReducedMotion()
                ? 4
                : 5 + Math.random() * 5;
            const drift = (Math.random() - 0.5) * 120;
            const rotation = Math.random() * 720 - 360;
            const opacity = 0.5 + Math.random() * 0.5;
            const blur = Math.random() > 0.7;

            if (blur) el.classList.add('flower-petal--blurred');
            if (Math.random() > 0.85) el.classList.add('flower-petal--near');

            el.style.left = `${startX}%`;
            el.style.transform = `scale(${size}) rotate(${Math.random() * 360}deg)`;
            el.style.opacity = opacity;

            this.container.appendChild(el);

            const petal = { el, startX, drift, rotation, duration };
            this.petals.push(petal);

            if (prefersReducedMotion()) {
                el.style.transition = `transform ${duration}s linear, opacity 0.5s ease`;
                requestAnimationFrame(() => {
                    el.style.transform = `translateY(110vh) translateX(${drift * 0.3}px) rotate(${rotation * 0.3}deg) scale(${size})`;
                    el.style.opacity = '0';
                });
                setTimeout(() => this._recyclePetal(petal), duration * 1000);
            } else {
                el.animate([
                    {
                        transform: `translateY(-40px) translateX(0) rotate(0deg) scale(${size})`,
                        opacity: opacity
                    },
                    {
                        transform: `translateY(105vh) translateX(${drift}px) rotate(${rotation}deg) scale(${size})`,
                        opacity: 0
                    }
                ], {
                    duration: duration * 1000,
                    easing: 'cubic-bezier(0.25, 0.46, 0.45, 0.94)',
                    fill: 'forwards'
                }).onfinish = () => this._recyclePetal(petal);
            }
        }

        _recyclePetal(petal) {
            const idx = this.petals.indexOf(petal);
            if (idx > -1) this.petals.splice(idx, 1);
            if (petal.el.parentNode) petal.el.remove();
        }
    }

    /* ------------------------------------------------------------------ */
    /* SpiritualSplashScreen                                              */
    /* ------------------------------------------------------------------ */
    class SpiritualSplashScreen {
        constructor() {
            this.splash = document.getElementById('spiritual-splash');
            this.appShell = document.getElementById('app-shell');
            this.skipBtn = document.getElementById('splash-skip');
            this.petalContainer = document.getElementById('splash-petals');
            this.petalAnim = null;
            this.dismissTimer = null;
            this.dismissed = false;
        }

        init() {
            if (!this.splash || !this.appShell) return;

            const alreadySeen = sessionStorage.getItem(SPLASH_KEY) === '1';

            if (alreadySeen) {
                this._revealApp(true);
                return;
            }

            this.appShell.classList.add('app-shell--splash-pending');
            this.splash.classList.add('is-active');
            this.splash.setAttribute('aria-hidden', 'false');

            this._createParticles();
            this.petalAnim = new FlowerPetalAnimation(this.petalContainer, {
                maxPetals: MAX_PETALS,
                intensity: 1.2
            });

            setTimeout(() => this.petalAnim.start(), 800);

            if (this.skipBtn) {
                this.skipBtn.addEventListener('click', () => this.dismiss());
            }

            this.dismissTimer = setTimeout(
                () => this.dismiss(),
                prefersReducedMotion() ? 1500 : SPLASH_DURATION_MS
            );
        }

        _createParticles() {
            const container = document.getElementById('splash-particles');
            if (!container || prefersReducedMotion()) return;
            for (let i = 0; i < 12; i++) {
                const p = document.createElement('div');
                p.className = 'spiritual-splash__particle';
                p.style.left = `${Math.random() * 100}%`;
                p.style.top = `${Math.random() * 100}%`;
                p.style.animationDelay = `${Math.random() * 4}s`;
                p.style.animationDuration = `${4 + Math.random() * 4}s`;
                container.appendChild(p);
            }
        }

        dismiss() {
            if (this.dismissed) return;
            this.dismissed = true;

            if (this.dismissTimer) clearTimeout(this.dismissTimer);
            if (this.petalAnim) this.petalAnim.stop();

            sessionStorage.setItem(SPLASH_KEY, '1');

            this.splash.classList.add('is-hidden');
            this.splash.setAttribute('aria-hidden', 'true');
            this._revealApp(false);

            setTimeout(() => {
                if (this.splash.parentNode) {
                    this.splash.style.display = 'none';
                }
            }, 900);
        }

        _revealApp(instant) {
            this.appShell.classList.remove('app-shell--splash-pending');
            if (instant) {
                this.appShell.style.opacity = '1';
                this.appShell.style.transform = 'none';
                if (this.splash) this.splash.style.display = 'none';
            } else {
                requestAnimationFrame(() => {
                    this.appShell.classList.add('app-shell--revealed');
                });
            }
            initDashboardAnimations();
        }
    }

    /* ------------------------------------------------------------------ */
    /* Dashboard micro-interactions                                       */
    /* ------------------------------------------------------------------ */
    function initDashboardAnimations() {
        document.querySelectorAll('.card--animate-in').forEach((card, i) => {
            setTimeout(() => card.classList.add('is-visible'), prefersReducedMotion() ? 0 : i * 80);
        });

        document.querySelectorAll('.btn-primary').forEach(btn => {
            btn.addEventListener('click', function (e) {
                const rect = this.getBoundingClientRect();
                this.style.setProperty('--ripple-x', `${((e.clientX - rect.left) / rect.width) * 100}%`);
                this.style.setProperty('--ripple-y', `${((e.clientY - rect.top) / rect.height) * 100}%`);
            });
        });

        const dashPetals = document.getElementById('dashboard-petals');
        if (dashPetals && !prefersReducedMotion()) {
            const ambient = new FlowerPetalAnimation(dashPetals, {
                maxPetals: 8,
                intensity: 0.4
            });
            ambient.start();
        }
    }

    /* ------------------------------------------------------------------ */
    /* Boot                                                               */
    /* ------------------------------------------------------------------ */
    document.addEventListener('DOMContentLoaded', () => {
        const splash = new SpiritualSplashScreen();
        splash.init();

        if (sessionStorage.getItem(SPLASH_KEY) === '1') {
            initDashboardAnimations();
        }
    });

    window.FlowerPetalAnimation = FlowerPetalAnimation;
    window.SpiritualSplashScreen = SpiritualSplashScreen;
})();
