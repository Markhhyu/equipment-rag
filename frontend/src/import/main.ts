import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import '../styles/base.css'
import './import.css'
import ImportApp from './ImportApp.vue'

createApp(ImportApp).use(ElementPlus).mount('#app')
