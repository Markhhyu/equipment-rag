import 'element-plus/dist/index.css'
import '../styles/base.css'
import './knowledge.css'
import './quality.css'
import KnowledgeApp from './KnowledgeApp.vue'
import { mountProtectedPage } from '../shared/bootstrap'

mountProtectedPage(KnowledgeApp, 'admin')
