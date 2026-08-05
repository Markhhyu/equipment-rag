import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import '../styles/base.css'
import './apps.css'
import AppsApp from './AppsApp.vue'

createApp(AppsApp).use(ElementPlus).mount('#app')
