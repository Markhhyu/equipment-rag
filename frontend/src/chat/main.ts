import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import '../styles/base.css'
import './chat.css'
import ChatApp from './ChatApp.vue'

createApp(ChatApp).use(ElementPlus).mount('#app')
