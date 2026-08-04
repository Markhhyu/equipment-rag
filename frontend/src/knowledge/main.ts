import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import '../styles/base.css'
import './knowledge.css'
import KnowledgeApp from './KnowledgeApp.vue'

createApp(KnowledgeApp).use(ElementPlus).mount('#app')
