import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import '../styles/base.css'
import './workflow.css'
import WorkflowApp from './WorkflowApp.vue'

createApp(WorkflowApp).use(ElementPlus).mount('#app')
