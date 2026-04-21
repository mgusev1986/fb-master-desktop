import re
from pathlib import Path

# Внимание: актуальный лендинг в проде — URL / (шаблон `templates/promo2.html`; /landing редиректит на /)
# и переводы `static/locales/promo2/*.json`. Запуск этого скрипта перезапишет
# шаблон устаревшей версией — используйте только если намеренно откатываете.

html_content = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="FB Master — ультра-премиум настольное приложение для автоматизации Facebook. AI-рассылки, парсер, Messenger 2, CRM и сценарии. Для тех, кто строит бизнес на уровне люкс.">
    <title>FB Master • Ультра-премиум автоматизация Facebook</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Playfair+Display:wght@500;600&display=swap');
        
        :root {
            --gold: #f5d06e;
        }
        
        * {
            transition-property: all;
            transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1);
            transition-duration: 250ms;
        }
        
        body {
            font-family: 'Inter', system-ui, sans-serif;
        }
        
        .heading {
            font-family: 'Playfair Display', sans-serif;
            letter-spacing: -0.04em;
        }
        
        .hero-bg {
            background: radial-gradient(circle at 30% 20%, #111111 0%, #0a0a0a 45%, #000000 100%);
        }
        
        .navbar {
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
        }
        
        .gold-gradient {
            background: linear-gradient(90deg, #f5d06e, #e8b93d);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .luxury-card {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(245, 208, 110, 0.2);
        }
        
        .luxury-card:hover {
            border-color: #f5d06e;
            box-shadow: 0 30px 60px -15px rgba(245, 208, 110, 0.3);
            transform: translateY(-8px);
        }
        
        .mockup-float {
            animation: luxury-float 6s ease-in-out infinite;
        }
        
        @keyframes luxury-float {
            0%, 100% { transform: translateY(0px) rotate(0deg); }
            50% { transform: translateY(-30px) rotate(3deg); }
        }
        
        .testimonial-card {
            box-shadow: 0 20px 40px -15px rgba(0,0,0,0.4);
        }
        
        .cta-button {
            background: linear-gradient(90deg, #f5d06e, #e8b93d);
            color: #111;
            box-shadow: 0 15px 35px -10px #f5d06e;
        }
        
        .cta-button:hover {
            box-shadow: 0 25px 45px -10px #f5d06e;
            transform: scale(1.05);
        }
    </style>
</head>
<body class="bg-black text-white overflow-x-hidden">
    <!-- NAVBAR -->
    <nav class="navbar fixed top-0 left-0 right-0 z-50 bg-black/90 border-b border-white/10">
        <div class="max-w-screen-2xl mx-auto px-10 py-6 flex items-center justify-between">
            <div class="flex items-center gap-x-4">
                <div class="w-9 h-9 bg-gradient-to-br from-[#f5d06e] to-amber-400 rounded-3xl flex items-center justify-center text-black text-3xl shadow-lg"><i class="fa-brands fa-facebook-f text-xl"></i></div>
                <h1 class="heading text-4xl tracking-[-1px]">FB Master</h1>
            </div>
            
            <div class="hidden md:flex items-center gap-x-10 text-base font-medium">
                <a href="#features" class="hover:text-[#f5d06e]">Возможности</a>
                <a href="#ai" class="hover:text-[#f5d06e]">ИИ</a>
                <a href="#business" class="hover:text-[#f5d06e]">Для бизнеса</a>
                <a href="#app" class="hover:text-[#f5d06e]">Приложение</a>
                <a href="#testimonials" class="hover:text-[#f5d06e]">Отзывы</a>
            </div>
            
            <div class="flex items-center gap-x-5">
                <a href="https://socmaster.pro/buy" 
                   class="cta-button px-9 py-4 rounded-3xl font-semibold text-lg flex items-center gap-x-3">
                    <i class="fa-solid fa-key"></i>
                    Купить лицензию
                </a>
                <a href="https://socmaster.pro/api/public/desktop-update" 
                   onclick="showUpdate()"
                   class="px-7 py-4 border border-white/30 hover:border-[#f5d06e] rounded-3xl flex items-center text-lg">
                    Скачать 1.5.2
                </a>
                <button onclick="toggleMenu()" class="md:hidden text-3xl">
                    <i class="fa-solid fa-bars"></i>
                </button>
            </div>
        </div>
        
        <!-- Mobile menu -->
        <div id="mobileMenu" class="hidden md:hidden bg-black border-t border-white/10 px-8 py-8">
            <a href="#features" class="block py-4 text-lg">Возможности</a>
            <a href="#ai" class="block py-4 text-lg">ИИ</a>
            <a href="#business" class="block py-4 text-lg">Для бизнеса</a>
            <a href="#app" class="block py-4 text-lg">Приложение</a>
            <a href="#testimonials" class="block py-4 text-lg">Отзывы</a>
            <div class="pt-6 border-t border-white/10">
                <a href="https://socmaster.pro/buy" class="cta-button block text-center py-6 rounded-3xl text-xl font-semibold">Купить сейчас</a>
            </div>
        </div>
    </nav>

    <!-- HERO -->
    <header class="hero-bg flex items-center pt-32 pb-20">
        <div class="max-w-screen-2xl mx-auto px-10 grid md:grid-cols-12 gap-x-16 items-center">
            <div class="md:col-span-7">
                <div class="inline-flex items-center bg-white/10 text-xs font-medium px-6 py-3 rounded-3xl border border-white/20 mb-8 backdrop-blur-3xl">
                    <span class="text-[#f5d06e] mr-2"><i class="fa-solid fa-circle text-[8px] animate-pulse"></i></span>
                    УЛЬТРА-ПРЕМИУМ • ВЕРСИЯ 1.5.2 • 2026
                </div>
                
                <h1 class="heading text-6xl md:text-[82px] leading-[1.05] tracking-[-3px]">
                    Ваш Facebook.<br>
                    Теперь <span class="gold-gradient">на уровне люкс</span>.
                </h1>
                
                <p class="text-2xl md:text-3xl text-white/80 mt-8 max-w-2xl">
                    Премиум-настольное приложение с искусственным интеллектом.<br>
                    Парсер, Messenger, CRM, AI-рассылки и сценарии — всё для тех, кто строит настоящий бизнес.
                </p>
                
                <div class="mt-14 flex flex-wrap gap-6">
                    <a href="https://socmaster.pro/buy" 
                       class="cta-button text-2xl md:text-3xl px-12 md:px-16 py-6 md:py-8 rounded-3xl flex items-center justify-center gap-x-5 font-semibold shadow-2xl">
                        Получить ключ
                        <i class="fa-solid fa-arrow-right text-2xl"></i>
                    </a>
                    
                    <a href="#app" 
                       class="px-10 py-6 md:py-8 text-xl md:text-2xl font-medium border border-white/30 hover:border-[#f5d06e] rounded-3xl flex items-center gap-x-4">
                        Посмотреть приложение
                    </a>
                </div>
                
                <div class="mt-16 flex flex-col md:flex-row gap-y-6 items-start md:items-center gap-x-12 text-sm">
                    <div class="flex items-center gap-x-3">
                        <i class="fa-solid fa-lock text-3xl text-[#f5d06e]"></i>
                        <div>Работает только у вас<br><span class="text-white/60">Полная приватность</span></div>
                    </div>
                    <div class="flex items-center gap-x-3">
                        <i class="fa-solid fa-robot text-3xl text-[#f5d06e]"></i>
                        <div>Встроенный ИИ<br><span class="text-white/60">OpenAI + Gemini + Ollama</span></div>
                    </div>
                    <div class="flex items-center gap-x-3">
                        <i class="fa-brands fa-apple text-3xl text-[#f5d06e]"></i>
                        <div>macOS • Windows<br><span class="text-white/60">Элегантно и мощно</span></div>
                    </div>
                </div>
                
                <div class="mt-20 text-white/60 flex flex-col md:flex-row items-start md:items-center gap-y-4 gap-x-8 text-sm">
                    <div>Уже 340+ предпринимателей</div>
                    <div class="hidden md:block h-px w-12 bg-white/30"></div>
                    <div class="flex items-center">Средний рост лидов ×4,7</div>
                </div>
            </div>
            
            <!-- Premium mockup -->
            <div class="md:col-span-5 relative mt-16 md:mt-0">
                <div class="mockup-float relative mx-auto max-w-[480px]">
                    <div class="bg-gradient-to-br from-[#1c1c1c] to-black rounded-[2.8rem] border border-[#f5d06e]/30 p-5 shadow-2xl">
                        <div class="bg-black rounded-3xl overflow-hidden">
                            <div class="px-8 py-5 flex justify-between items-center text-sm border-b border-white/10">
                                <div class="flex items-center gap-x-3">
                                    <span class="text-[#f5d06e]"><i class="fa-solid fa-code mr-2"></i>FB Master</span>
                                    <span class="text-xs bg-white/10 px-4 py-1 rounded-3xl">v1.5.2</span>
                                </div>
                                <div class="text-emerald-400 flex items-center text-xs">
                                    <span class="w-3 h-3 bg-emerald-400 rounded-full animate-pulse mr-2"></span>
                                    Messenger • LIVE
                                </div>
                            </div>
                            <div class="h-96 flex items-center justify-center relative">
                                <div class="text-center">
                                    <div class="text-8xl mb-6 text-emerald-400"><i class="fa-solid fa-arrow-trend-up text-transparent bg-clip-text bg-gradient-to-r from-emerald-400 to-emerald-200"></i></div>
                                    <div class="text-5xl md:text-6xl font-semibold">+14 392 лида</div>
                                    <div class="text-[#f5d06e] text-2xl md:text-3xl mt-4">за 30 дней</div>
                                </div>
                                <div class="absolute top-10 right-10 bg-white/10 px-6 py-3 rounded-3xl text-sm backdrop-blur flex items-center gap-x-2 border border-white/10">
                                    <i class="fa-solid fa-wand-magic-sparkles text-[#f5d06e]"></i>
                                    AI-агент работает
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </header>

    <!-- TRUST BAR -->
    <div class="py-12 bg-black border-b border-white/10">
        <div class="max-w-screen-2xl mx-auto px-10 flex flex-wrap justify-center md:justify-between gap-x-12 gap-y-8 text-sm opacity-75 items-center font-medium">
            <div class="flex items-center gap-x-3"><i class="fa-brands fa-apple text-3xl"></i> macOS arm64</div>
            <div class="flex items-center gap-x-3"><i class="fa-brands fa-windows text-3xl"></i> Windows</div>
            <div class="flex items-center gap-x-3"><i class="fa-solid fa-shield-halved text-2xl text-[#f5d06e]"></i> NOWPayments • Лицензия</div>
            <div class="text-[#f5d06e] flex items-center gap-x-3"><i class="fa-brands fa-python text-2xl"></i> Playwright • Встроенный Python</div>
            <div class="flex items-center gap-x-3"><i class="fa-solid fa-brain text-2xl"></i> OpenAI • Gemini • Ollama</div>
        </div>
    </div>

    <!-- FOR BUSINESS -->
    <section id="business" class="max-w-screen-2xl mx-auto px-10 py-24 md:py-32">
        <div class="text-center mb-16 md:mb-24">
            <span class="uppercase text-sm tracking-[2px] text-[#f5d06e] font-semibold border border-[#f5d06e]/30 px-6 py-2 rounded-full">Для серьёзного бизнеса</span>
            <h2 class="heading text-5xl md:text-6xl mt-8">Кому нужен этот инструмент</h2>
        </div>
        <div class="grid md:grid-cols-3 gap-8">
            <div class="luxury-card rounded-[2rem] p-12 transition-transform duration-500 hover:-translate-y-4">
                <div class="text-6xl mb-8 text-[#f5d06e]"><i class="fa-solid fa-magnet text-transparent bg-clip-text bg-gradient-to-br from-[#f5d06e] to-amber-500"></i></div>
                <h3 class="text-3xl font-semibold">MLM и сетевой бизнес</h3>
                <p class="text-white/70 mt-6 text-xl leading-relaxed">Тёплые приглашения, AI-персонализация, прогрев и масштабирование партнёрской сети.</p>
            </div>
            <div class="luxury-card rounded-[2rem] p-12 transition-transform duration-500 hover:-translate-y-4">
                <div class="text-6xl mb-8 text-[#f5d06e]"><i class="fa-solid fa-briefcase text-transparent bg-clip-text bg-gradient-to-br from-[#f5d06e] to-amber-500"></i></div>
                <h3 class="text-3xl font-semibold">Малый и средний бизнес</h3>
                <p class="text-white/70 mt-6 text-xl leading-relaxed">Лидогенерация из Facebook, автоматические комментарии и CRM-воронка, которая закрывает сделки.</p>
            </div>
            <div class="luxury-card rounded-[2rem] p-12 transition-transform duration-500 hover:-translate-y-4">
                <div class="text-6xl mb-8 text-[#f5d06e]"><i class="fa-solid fa-building text-transparent bg-clip-text bg-gradient-to-br from-[#f5d06e] to-amber-500"></i></div>
                <h3 class="text-3xl font-semibold">Крупный бизнес и бренды</h3>
                <p class="text-white/70 mt-6 text-xl leading-relaxed">Масштабные сценарии, языковая сегментация, ротация аккаунтов и полный контроль трафика.</p>
            </div>
        </div>
    </section>

    <!-- FEATURES -->
    <section id="features" class="max-w-screen-2xl mx-auto px-10 py-24 md:py-32 bg-black">
        <h2 class="heading text-center text-5xl md:text-6xl mb-24">Инструменты уровня люкс</h2>
        <div class="grid md:grid-cols-2 lg:grid-cols-3 gap-10">
            <div class="group luxury-card rounded-[2rem] p-12 relative overflow-hidden">
                <div class="absolute -right-10 -top-10 w-40 h-40 bg-[#f5d06e]/10 rounded-full blur-3xl group-hover:bg-[#f5d06e]/20 transition-all"></div>
                <div class="text-5xl mb-8 text-[#f5d06e]"><i class="fa-solid fa-magnifying-glass"></i></div>
                <h3 class="text-3xl font-semibold">Интеллектуальный парсер</h3>
                <p class="text-white/70 mt-6">Друзья, подписчики, following, участники групп. Автоматическая сегментация по языку RU/EN/FOREIGN.</p>
            </div>
            <div class="group luxury-card rounded-[2rem] p-12 relative overflow-hidden">
                <div class="absolute -right-10 -top-10 w-40 h-40 bg-[#f5d06e]/10 rounded-full blur-3xl group-hover:bg-[#f5d06e]/20 transition-all"></div>
                <div class="text-5xl mb-8 text-[#f5d06e]"><i class="fa-solid fa-robot"></i></div>
                <h3 class="text-3xl font-semibold">AI-рассылка и комменты</h3>
                <p class="text-white/70 mt-6">Нейро-вариации, first-touch, персональные сообщения и комментарии под постами.</p>
            </div>
            <div class="group luxury-card rounded-[2rem] p-12 relative overflow-hidden">
                <div class="absolute -right-10 -top-10 w-40 h-40 bg-[#f5d06e]/10 rounded-full blur-3xl group-hover:bg-[#f5d06e]/20 transition-all"></div>
                <div class="text-5xl mb-8 text-[#f5d06e]"><i class="fa-solid fa-chart-column"></i></div>
                <h3 class="text-3xl font-semibold">Премиум CRM-воронка</h3>
                <p class="text-white/70 mt-6">Канбан-стадии, автоматические активности и история всех взаимодействий для 100% контроля.</p>
            </div>
            <div class="group luxury-card rounded-[2rem] p-12 relative overflow-hidden">
                <div class="absolute -right-10 -top-10 w-40 h-40 bg-[#f5d06e]/10 rounded-full blur-3xl group-hover:bg-[#f5d06e]/20 transition-all"></div>
                <div class="text-5xl mb-8 text-[#f5d06e]"><i class="fa-regular fa-calendar-days"></i></div>
                <h3 class="text-3xl font-semibold">Сценарии по дням + агент</h3>
                <p class="text-white/70 mt-6">Комбо-действия с автопилотом, throttle задержками и адаптацией под часовые пояса.</p>
            </div>
            <div class="group luxury-card rounded-[2rem] p-12 relative overflow-hidden">
                <div class="absolute -right-10 -top-10 w-40 h-40 bg-red-500/10 rounded-full blur-3xl group-hover:bg-red-500/20 transition-all"></div>
                <div class="text-5xl mb-8 text-red-500"><i class="fa-solid fa-fire"></i></div>
                <h3 class="text-3xl font-semibold">Прогрев аккаунтов</h3>
                <p class="text-white/70 mt-6">Natural warmup и мягкие кампании с автослотами 1–3 для защиты ваших аккаунтов от бана.</p>
            </div>
            <div class="group luxury-card rounded-[2rem] p-12 relative overflow-hidden">
                <div class="absolute -right-10 -top-10 w-40 h-40 bg-blue-500/10 rounded-full blur-3xl group-hover:bg-blue-500/20 transition-all"></div>
                <div class="text-5xl mb-8 text-blue-400"><i class="fa-brands fa-facebook-messenger"></i></div>
                <h3 class="text-3xl font-semibold">Интеграция Messenger</h3>
                <p class="text-white/70 mt-6">Живой Facebook Messenger внутри приложения. Работает параллельно с автоматизацией.</p>
            </div>
        </div>
    </section>

    <!-- AI SECTION -->
    <section id="ai" class="py-24 md:py-32 bg-gradient-to-b from-black via-[#0a0a0a] to-[#111] relative border-t border-white/5">
        <div class="absolute inset-0 bg-[url('https://www.transparenttextures.com/patterns/cubes.png')] opacity-[0.03]"></div>
        <div class="max-w-screen-2xl mx-auto px-10 relative">
            <div class="grid lg:grid-cols-12 gap-16 items-center">
                <div class="lg:col-span-5">
                    <span class="text-[#f5d06e] font-medium tracking-[2px] text-sm flex items-center border border-[#f5d06e]/30 px-5 py-2 rounded-full w-max"><i class="fa-solid fa-bolt mr-2"></i>НЕЙРОСЕТИ ВНУТРИ</span>
                    <h2 class="heading text-5xl md:text-6xl mt-8">ИИ, который понимает бизнес</h2>
                    <p class="text-2xl text-white/70 mt-8 leading-relaxed">Автоматические нейро-вариации текстов, умный подбор аудитории и персональные сообщения в реальном времени.</p>
                    <div class="mt-12 flex gap-4 flex-wrap">
                        <div class="bg-white/5 border border-white/10 px-6 py-4 rounded-2xl text-lg flex items-center gap-x-3"><i class="fa-solid fa-microchip text-[#f5d06e]"></i> OpenAI</div>
                        <div class="bg-white/5 border border-white/10 px-6 py-4 rounded-2xl text-lg flex items-center gap-x-3"><i class="fa-brands fa-google text-[#f5d06e]"></i> Google Gemini</div>
                        <div class="bg-white/5 border border-white/10 px-6 py-4 rounded-2xl text-lg flex items-center gap-x-3"><i class="fa-solid fa-cube text-[#f5d06e]"></i> Ollama <span class="text-white/40 text-sm ml-2">(локально)</span></div>
                    </div>
                </div>
                <div class="lg:col-span-7 luxury-card rounded-[2.5rem] p-12 md:p-20 relative overflow-hidden">
                    <div class="absolute inset-0 bg-gradient-to-br from-[#f5d06e]/5 to-transparent"></div>
                    <div class="text-8xl text-center drop-shadow-[0_0_30px_rgba(245,208,110,0.5)]"><i class="fa-solid fa-wand-magic-sparkles text-transparent bg-clip-text bg-gradient-to-r from-[#f5d06e] to-amber-300"></i></div>
                    <p class="text-center text-3xl mt-10 md:mt-12 max-w-2xl mx-auto leading-tight italic">«AI-агент анализирует профили и пишет сообщения, которые действительно конвертируют»</p>
                </div>
            </div>
        </div>
    </section>

    <!-- TESTIMONIALS CAROUSEL -->
    <section id="testimonials" class="py-24 md:py-32 bg-black border-t border-white/5">
        <div class="max-w-screen-2xl mx-auto px-4 md:px-10">
            <div class="flex flex-col md:flex-row justify-between items-start md:items-end mb-16 px-4 md:px-0">
                <h2 class="heading text-5xl md:text-6xl">Они уже автоматизируют</h2>
                <div class="flex gap-x-4 mt-8 md:mt-0">
                    <button onclick="prevSlide()" class="w-16 h-16 border border-white/20 rounded-full flex items-center justify-center text-xl hover:border-[#f5d06e] hover:bg-[#f5d06e]/10 transition-all"><i class="fa-solid fa-arrow-left"></i></button>
                    <button onclick="nextSlide()" class="w-16 h-16 border border-white/20 rounded-full flex items-center justify-center text-xl hover:border-[#f5d06e] hover:bg-[#f5d06e]/10 transition-all"><i class="fa-solid fa-arrow-right"></i></button>
                </div>
            </div>
            
            <div id="testimonialCarousel" class="relative overflow-hidden w-full cursor-grab active:cursor-grabbing">
                <div class="flex transition-transform duration-700 ease-in-out" id="slidesContainer">
                    <!-- REVIEWS -->
"""

reviews = [
    {
        "text": "FB Master — это как личный отдел продаж в Facebook. За первый месяц +187 новых клиентов. AI-рассылка просто огонь.",
        "name": "Алексей Морозов",
        "role": "Основатель MLM-проекта",
        "metric": "×6 рост лидов",
        "img": "men/32.jpg"
    },
    {
        "text": "Messenger + CRM изменили всё. Теперь я общаюсь с клиентами и одновременно запускаю автоматизацию. Лучший инструмент 2026 года.",
        "name": "Мария Ковалёва",
        "role": "SMM-директор крупного магазина",
        "metric": "Экономия 40 часов в неделю",
        "img": "women/44.jpg"
    },
    {
        "text": "The natural warmup algorithms saved our accounts. We run multi-account campaigns securely without blocks. Highly recommended.",
        "name": "Michael Harrison",
        "role": "Lead Generation Agency CEO",
        "metric": "Zero account bans",
        "img": "men/16.jpg"
    },
    {
        "text": "Парсер с языковой сегментацией и сценарии по дням — это уровень, которого нет ни у кого. Мой бизнес вырос в 4 раза.",
        "name": "Дмитрий Соколов",
        "role": "Владелец сети салонов",
        "metric": "+1 200 лидов в месяц",
        "img": "men/22.jpg"
    },
    {
        "text": "This app is an engineering marvel. It automates precisely like a human. Apple-level design is exactly what we needed.",
        "name": "Sarah Jenkins",
        "role": "Digital Marketing Strat",
        "metric": "ROI increased by 320%",
        "img": "women/61.jpg"
    },
    {
        "text": "The AI Agent sends personalized messages that look extremely natural. The client response rate has spiked massively.",
        "name": "Lin Wei",
        "role": "Global E-commerce Brand Owner",
        "metric": "+450% sales growth",
        "img": "women/21.jpg"
    },
    {
        "text": "Встроенная CRM позволяет вести тысячи переписок и не терять клиентов. Это прорыв в автоматизации социальных продаж.",
        "name": "Елена Волкова",
        "role": "Директор по развитию",
        "metric": "+85% конверсия в сделку",
        "img": "women/68.jpg"
    },
    {
        "text": "Our team used to spend days on manual outreach. Now FB Master does it automatically using AI. Beautiful app.",
        "name": "David Chen",
        "role": "B2B SaaS Sales Director",
        "metric": "2,000+ targeted connections",
        "img": "men/63.jpg"
    },
    {
        "text": "Сценарии по дням и ротация аккаунтов. FB Master делает то, на что раньше уходила целая команда из 5 человек.",
        "name": "Илья Карпов",
        "role": "Traffic Manager",
        "metric": "5000+ кликов из FB",
        "img": "men/55.jpg"
    },
    {
        "text": "Un nivel de automatización sorprendente. La interfaz es intuitiva y las integraciones de AI superaron todas las expectativas.",
        "name": "Isabella Ramos",
        "role": "Marketing Consultant",
        "metric": "x5 leads generation",
        "img": "women/54.jpg"
    }
]

for review in reviews:
    html_content += f'''
                    <!-- Slide -->
                    <div class="min-w-[100%] md:min-w-[50%] lg:min-w-[33.333%] px-3">
                        <div class="luxury-card bg-[#111]/80 rounded-[2rem] p-10 h-full flex flex-col justify-between">
                            <p class="text-xl italic font-light text-white/90 leading-relaxed mb-10">"{review['text']}"</p>
                            <div class="flex items-center gap-x-5 pt-6 border-t border-white/10">
                                <img src="https://randomuser.me/api/portraits/{review['img']}" alt="{review['name']}" class="w-14 h-14 rounded-full border border-white/20 object-cover grayscale-[20%]">
                                <div>
                                    <div class="font-semibold text-lg">{review['name']}</div>
                                    <div class="text-white/50 text-sm whitespace-nowrap overflow-hidden text-ellipsis max-w-[200px]">{review['role']}</div>
                                    <div class="text-[#f5d06e] font-medium text-sm mt-0.5"><i class="fa-solid fa-arrow-trend-up mr-1 text-[10px]"></i>{review['metric']}</div>
                                </div>
                            </div>
                        </div>
                    </div>
    '''

html_content += """
                </div>
            </div>
            
            <div class="flex justify-center mt-12 gap-x-3" id="dotsContainer">
                <!-- Dots generated by JS -->
            </div>
        </div>
    </section>

    <!-- DESKTOP APP -->
    <section id="app" class="max-w-screen-2xl mx-auto px-10 py-24 md:py-32 border-t border-b border-white/10">
        <div class="grid md:grid-cols-12 gap-16 items-center">
            <div class="md:col-span-6">
                <h2 class="heading text-5xl md:text-6xl">Элегантное настольное приложение</h2>
                <p class="mt-8 text-2xl text-white/70 leading-relaxed">Electron + встроенный Python. Работает только у вас на компьютере. Никаких облачных рисков и утечек данных.</p>
                <div class="mt-12 space-y-6 text-xl">
                    <div class="flex items-center gap-x-5 luxury-card px-8 py-5 rounded-2xl"><i class="fa-solid fa-cloud-arrow-down text-[#f5d06e] text-2xl w-8 text-center"></i> macOS arm64 DMG + Windows bundle</div>
                    <div class="flex items-center gap-x-5 luxury-card px-8 py-5 rounded-2xl"><i class="fa-brands fa-chrome text-[#f5d06e] text-2xl w-8 text-center"></i> Встроенный Chromium и Playwright</div>
                    <div class="flex items-center gap-x-5 luxury-card px-8 py-5 rounded-2xl"><i class="fa-solid fa-arrows-rotate text-[#f5d06e] text-2xl w-8 text-center"></i> Автообновления и привязка устройства</div>
                </div>
            </div>
            <div class="md:col-span-6">
                <div class="mockup-float bg-black rounded-[3rem] border border-white/20 p-4 max-w-lg mx-auto shadow-[0_30px_60px_-15px_rgba(255,255,255,0.1)]">
                    <div class="rounded-[2.5rem] overflow-hidden bg-gradient-to-b from-[#111] to-black border border-white/5 flex flex-col items-center justify-center text-center py-24 text-white/80">
                        <i class="fa-solid fa-desktop mb-8 block text-[#f5d06e] text-8xl drop-shadow-[0_0_20px_rgba(245,208,110,0.4)]"></i>
                        <span class="text-3xl font-medium tracking-tight">FB Master Desktop</span>
                        <span class="text-sm bg-white/10 px-4 py-1.5 rounded-full mt-6 backdrop-blur">Версия 1.5.2</span>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <!-- HOW IT WORKS -->
    <section class="max-w-screen-2xl mx-auto px-10 py-24 md:py-32">
        <h2 class="heading text-5xl md:text-6xl text-center mb-24">3 шага — и вы уже зарабатываете</h2>
        <div class="grid md:grid-cols-3 gap-12 relative">
            <div class="hidden md:block absolute top-[4.5rem] left-[20%] right-[20%] h-0.5 bg-gradient-to-r from-[#f5d06e]/50 via-[#f5d06e] to-[#f5d06e]/50 opacity-20"></div>
            <div class="text-center group">
                <div class="mx-auto w-24 h-24 flex items-center justify-center bg-[#f5d06e] text-black text-4xl rounded-full shadow-[0_15px_40px_-10px_rgba(245,208,110,0.5)] mb-10 group-hover:scale-110 transition-transform relative z-10"><i class="fa-solid fa-download"></i></div>
                <h3 class="text-3xl font-semibold">Скачайте приложение</h3>
                <p class="text-xl text-white/70 mt-6 leading-relaxed">Система сама установит все зависимости: Python, Chromium и Node.js за 60 секунд.</p>
            </div>
            <div class="text-center group">
                <div class="mx-auto w-24 h-24 flex items-center justify-center bg-[#f5d06e] text-black text-4xl rounded-full shadow-[0_15px_40px_-10px_rgba(245,208,110,0.5)] mb-10 group-hover:scale-110 transition-transform relative z-10"><i class="fa-solid fa-key"></i></div>
                <h3 class="text-3xl font-semibold">Активируйте ключ</h3>
                <p class="text-xl text-white/70 mt-6 leading-relaxed">Купите на socmaster.pro и введите ключ. Приложение привяжется к вашему железу.</p>
            </div>
            <div class="text-center group">
                <div class="mx-auto w-24 h-24 flex items-center justify-center bg-[#f5d06e] text-black text-4xl rounded-full shadow-[0_15px_40px_-10px_rgba(245,208,110,0.5)] mb-10 group-hover:scale-110 transition-transform relative z-10"><i class="fa-solid fa-rocket"></i></div>
                <h3 class="text-3xl font-semibold">Запускайте рост</h3>
                <p class="text-xl text-white/70 mt-6 leading-relaxed">Добавляйте аккаунты Facebook, включайте AI-агента и запускайте воронку.</p>
            </div>
        </div>
    </section>

    <!-- FINAL CTA -->
    <div class="max-w-screen-2xl mx-auto px-10 py-32 md:py-40 text-center bg-gradient-to-b from-black to-[#0a0a0a] border-t border-white/10 relative overflow-hidden">
        <div class="absolute top-0 left-1/2 -translate-x-1/2 w-[800px] h-[300px] bg-[#f5d06e] opacity-[0.03] blur-[120px] rounded-full pointer-events-none"></div>
        <h2 class="heading text-6xl md:text-7xl max-w-5xl mx-auto relative z-10">Готовы превратить Facebook в главный канал бизнеса?</h2>
        <p class="text-2xl md:text-3xl text-white/60 mt-8 font-light relative z-10">Премиум-лицензия + приложение ждут вас.</p>
        
        <div class="relative z-10">
            <a href="https://socmaster.pro/buy" 
               class="inline-flex mt-16 text-2xl md:text-3xl px-16 py-8 bg-gradient-to-r from-[#f5d06e] to-amber-500 text-black hover:to-amber-400 rounded-full items-center gap-x-6 font-semibold shadow-[0_20px_50px_-15px_rgba(245,208,110,0.4)] transition-all hover:scale-105 active:scale-95">
                Купить FB Master сейчас
                <i class="fa-solid fa-arrow-right text-2xl"></i>
            </a>
        </div>
        
        <div class="text-sm font-medium text-white/50 mt-14 flex flex-wrap justify-center items-center gap-x-8 gap-y-4 relative z-10">
            <span class="flex items-center"><i class="fa-solid fa-shield-check mr-2 text-[#f5d06e]"></i>Безопасная крипто-оплата</span>
            <span class="flex items-center"><i class="fa-solid fa-infinity mr-2 text-[#f5d06e]"></i>Обновления 1.4.x</span>
            <span class="flex items-center"><i class="fa-solid fa-headset mr-2 text-[#f5d06e]"></i>Telegram-поддержка 24/7</span>
        </div>
    </div>

    <!-- FOOTER -->
    <footer class="bg-[#050505] py-16 border-t border-white/10">
        <div class="max-w-screen-2xl mx-auto px-10 text-white/50 text-sm">
            <div class="flex flex-col md:flex-row justify-between items-center gap-y-8">
                <div class="flex items-center gap-x-4">
                    <div class="w-12 h-12 bg-white/5 border border-white/10 rounded-2xl flex items-center justify-center text-xl text-white"><i class="fa-brands fa-facebook-f"></i></div>
                    <div class="heading text-3xl text-white">FB Master</div>
                </div>
                <div class="flex gap-x-8 font-medium text-base">
                    <a href="https://socmaster.pro" class="hover:text-[#f5d06e] transition-colors">socmaster.pro</a>
                    <a href="#features" class="hover:text-[#f5d06e] transition-colors">Возможности</a>
                    <a href="#testimonials" class="hover:text-[#f5d06e] transition-colors">Отзывы</a>
                </div>
                <div>© 2026 FB Master • The Premium Desktop Suite</div>
            </div>
        </div>
    </footer>

    <script>
        // Carousel logic
        let currentSlide = 0;
        const container = document.getElementById('slidesContainer');
        const dotsContainer = document.getElementById('dotsContainer');
        const cards = container.children;
        
        function getItemsPerView() {
            if(window.innerWidth >= 1024) return 3; // lg
            if(window.innerWidth >= 768) return 2;  // md
            return 1;                               // sm
        }
        
        let itemsPerView = getItemsPerView();
        let totalSlides = Math.ceil(cards.length - itemsPerView + 1);
        
        function initCarousel() {
            dotsContainer.innerHTML = '';
            for(let i=0; i<totalSlides; i++) {
                const dot = document.createElement('button');
                dot.className = `w-12 h-2 rounded-full transition-colors ${i===currentSlide ? 'bg-[#f5d06e]' : 'bg-white/20 hover:bg-white/40'}`;
                dot.onclick = () => { currentSlide = i; updateCarousel(); };
                dotsContainer.appendChild(dot);
            }
            updateCarousel();
        }
        
        function updateCarousel() {
            if(currentSlide >= totalSlides) currentSlide = totalSlides - 1;
            if(currentSlide < 0) currentSlide = 0;
            
            let percent = currentSlide * (100 / itemsPerView);
            // Quick fix for the offset logic for multiple items
            let slideWidthStr = getComputedStyle(cards[0]).width;
            container.style.transform = `translateX(calc(-${currentSlide} * ${slideWidthStr}))`;
            
            Array.from(dotsContainer.children).forEach((dot, index) => {
                dot.className = `w-12 h-2 rounded-full transition-colors ${index===currentSlide ? 'bg-[#f5d06e]' : 'bg-white/20 hover:bg-white/40'}`;
            });
        }
        
        function nextSlide() {
            currentSlide = (currentSlide + 1) % totalSlides;
            updateCarousel();
        }
        
        function prevSlide() {
            currentSlide = (currentSlide - 1 + totalSlides) % totalSlides;
            updateCarousel();
        }
        
        initCarousel();
        
        // Auto-slide
        let slideInterval = setInterval(nextSlide, 5000);
        
        const carousel = document.getElementById('testimonialCarousel');
        carousel.addEventListener('mouseenter', () => clearInterval(slideInterval));
        carousel.addEventListener('mouseleave', () => {
            slideInterval = setInterval(nextSlide, 5000);
        });
        
        // Swipe logic for touch devices
        let startX = 0;
        let isDragging = false;
        
        carousel.addEventListener('touchstart', (e) => {
            startX = e.touches[0].clientX;
            isDragging = true;
            clearInterval(slideInterval);
        }, {passive: true});
        
        carousel.addEventListener('touchmove', (e) => {
            if(!isDragging) return;
        }, {passive: true});
        
        carousel.addEventListener('touchend', (e) => {
            if(!isDragging) return;
            let endX = e.changedTouches[0].clientX;
            let diff = startX - endX;
            
            if(Math.abs(diff) > 50) {
                if(diff > 0) nextSlide();
                else prevSlide();
            }
            isDragging = false;
            slideInterval = setInterval(nextSlide, 5000);
        });
        
        function toggleMenu() {
            const menu = document.getElementById('mobileMenu');
            menu.classList.toggle('hidden');
        }
        
        function showUpdate() {
            alert('✅ Вы уже на версии 1.5.2 — самой актуальной!\n\nМанифест обновлений: https://socmaster.pro/api/public/desktop-update');
        }
        
        window.addEventListener('resize', () => {
            let newItemsPerView = getItemsPerView();
            if(newItemsPerView !== itemsPerView) {
                itemsPerView = newItemsPerView;
                totalSlides = Math.ceil(cards.length - itemsPerView + 1);
                currentSlide = 0;
                initCarousel();
            } else {
                updateCarousel();
            }
        });
    </script>
</body>
</html>
"""

_out = Path(__file__).resolve().parent / "templates" / "promo2.html"
with open(_out, "w", encoding="utf-8") as f:
    f.write(html_content)
