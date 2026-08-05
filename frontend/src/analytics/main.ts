import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import '../styles/base.css'
import './analytics.css'
import AnalyticsApp from './AnalyticsApp.vue'

createApp(AnalyticsApp).use(ElementPlus).mount('#app')
